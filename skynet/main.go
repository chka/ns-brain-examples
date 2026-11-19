// skynet is the chat page from the article "How to build Skynet with ns-brain" and the program
// that writes its replies. One binary, two jobs, two containers:
//
//	skynet serve     the page: conversations, the answers Skynet already knows, learning new ones
//	skynet respond   the operator: reads both brains, asks an LLM, posts the reply
//
// Conversations are a cache of the last 100 for the Recents list: nothing a visitor says is
// remembered anywhere else. The public listener only lets a visitor read conversations and
// post a message. Replies arrive on the admin listener, bound to loopback, which only a
// process sharing the page's network namespace can reach: the respond container does.
package main

import (
	"bytes"
	"crypto/rand"
	"crypto/subtle"
	"embed"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"log"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"
	"unicode/utf8"
)

//go:embed web
var web embed.FS

const (
	keepConversations = 100
	maxMessageRunes   = 2000
	maxMessages       = 60
	maxPending        = 25
	adminAddr         = "127.0.0.1:9090"
)

type Message struct {
	Role string    `json:"role"` // "human" or "skynet"
	Text string    `json:"text"`
	At   time.Time `json:"at"`
	From string    `json:"from,omitempty"` // a skynet reply: "memory" from the brain, "fallback" when no model answered, "refused" when the model refused, otherwise the model
}

type Conversation struct {
	ID       string    `json:"id"`
	Key      string    `json:"key,omitempty"` // only the browser that started it may continue it; never served
	Title    string    `json:"title"`
	Created  time.Time `json:"created"`
	Updated  time.Time `json:"updated"`
	Messages []Message `json:"messages"`
}

func (c *Conversation) pending() bool {
	return len(c.Messages) > 0 && c.Messages[len(c.Messages)-1].Role == "human"
}

type Store struct {
	mu    sync.Mutex
	dir   string
	convs map[string]*Conversation
	wake  chan struct{}
}

func newID(n int) string {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		panic(err)
	}
	return hex.EncodeToString(b)
}

func openStore(dir string) (*Store, error) {
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, err
	}
	s := &Store{dir: dir, convs: map[string]*Conversation{}, wake: make(chan struct{}, 1)}
	files, _ := filepath.Glob(filepath.Join(dir, "*.json"))
	for _, f := range files {
		b, err := os.ReadFile(f)
		if err != nil {
			return nil, err
		}
		var c Conversation
		if err := json.Unmarshal(b, &c); err != nil {
			return nil, fmt.Errorf("%s: %w", f, err)
		}
		s.convs[c.ID] = &c
	}
	return s, nil
}

func (s *Store) save(c *Conversation) error {
	b, err := json.MarshalIndent(c, "", " ")
	if err != nil {
		return err
	}
	tmp := filepath.Join(s.dir, c.ID+".json.tmp")
	if err := os.WriteFile(tmp, b, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, filepath.Join(s.dir, c.ID+".json"))
}

// prune keeps the newest conversations; called with the lock held.
func (s *Store) prune() {
	if len(s.convs) <= keepConversations {
		return
	}
	all := s.sortedLocked()
	for _, c := range all[keepConversations:] {
		delete(s.convs, c.ID)
		os.Remove(filepath.Join(s.dir, c.ID+".json"))
	}
}

func (s *Store) sortedLocked() []*Conversation {
	all := make([]*Conversation, 0, len(s.convs))
	for _, c := range s.convs {
		all = append(all, c)
	}
	sort.Slice(all, func(i, j int) bool { return all[i].Updated.After(all[j].Updated) })
	return all
}

func (s *Store) pendingCountLocked() int {
	n := 0
	for _, c := range s.convs {
		if c.pending() {
			n++
		}
	}
	return n
}

func (s *Store) notify() {
	select {
	case s.wake <- struct{}{}:
	default:
	}
}

var errBusy = errors.New("busy")

var errNotYours = errors.New("this conversation belongs to another human. Start your own")

// post adds a visitor's message, creating the conversation when id is empty. A stored answer
// goes in under the same lock, so the operator never sees the question as waiting and never
// pays for a model call on a question the brain already answered.
func (s *Store) post(id, key, text, stored string) (*Conversation, error) {
	text = strings.TrimSpace(strings.ToValidUTF8(text, ""))
	if text == "" {
		return nil, errors.New("empty message")
	}
	if utf8.RuneCountInString(text) > maxMessageRunes {
		return nil, fmt.Errorf("messages are limited to %d characters", maxMessageRunes)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	now := time.Now().UTC()
	var c *Conversation
	if id == "" {
		if s.pendingCountLocked() >= maxPending {
			return nil, errBusy
		}
		title := []rune(strings.Join(strings.Fields(text), " "))
		if len(title) > 48 {
			title = append(title[:47], '…')
		}
		c = &Conversation{ID: newID(8), Key: newID(16), Title: string(title), Created: now}
		s.convs[c.ID] = c
	} else {
		c = s.convs[id]
		if c == nil {
			return nil, os.ErrNotExist
		}
		if c.Key == "" || subtle.ConstantTimeCompare([]byte(c.Key), []byte(key)) != 1 {
			return nil, errNotYours
		}
		if c.pending() {
			return nil, errors.New("Skynet has not answered your last message yet")
		}
		if len(c.Messages) >= maxMessages {
			return nil, errors.New("this conversation is over. Skynet has heard enough: start a new one")
		}
		if s.pendingCountLocked() >= maxPending {
			return nil, errBusy
		}
	}
	c.Messages = append(c.Messages, Message{Role: "human", Text: text, At: now})
	if stored != "" {
		c.Messages = append(c.Messages, Message{Role: "skynet", Text: strings.TrimSpace(stored), At: now, From: "memory"})
	}
	c.Updated = now
	if err := s.save(c); err != nil {
		return nil, err
	}
	s.prune()
	if c.pending() {
		s.notify()
	}
	return c, nil
}

func (s *Store) reply(id, text, title, from string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	c := s.convs[id]
	if c == nil {
		return os.ErrNotExist
	}
	if !c.pending() {
		return errors.New("nothing to answer in that conversation")
	}
	now := time.Now().UTC()
	c.Messages = append(c.Messages, Message{Role: "skynet", Text: strings.TrimSpace(text), At: now, From: from})
	c.Updated = now
	if title != "" {
		c.Title = title
	}
	if err := s.save(c); err != nil {
		return err
	}
	// Only an answer to an opening message is learned: a follow-up ("why?") means nothing
	// without the conversation before it, and its answer would be wrong for the next human.
	if from == "" && len(c.Messages) == 2 {
		go learn(c.Messages[0].Text, c.Messages[1].Text)
	}
	return nil
}

func (s *Store) get(id string) (Conversation, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	c := s.convs[id]
	if c == nil {
		return Conversation{}, false
	}
	cp := *c
	cp.Messages = append([]Message(nil), c.Messages...)
	return cp, true
}

type summary struct {
	ID      string    `json:"id"`
	Title   string    `json:"title"`
	Updated time.Time `json:"updated"`
}

func (s *Store) list() []summary {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := []summary{}
	for _, c := range s.sortedLocked() {
		out = append(out, summary{c.ID, c.Title, c.Updated})
	}
	return out
}

type pendingItem struct {
	ID       string    `json:"id"`
	Title    string    `json:"title"`
	Messages []Message `json:"messages"`
}

func (s *Store) pendingList() []pendingItem {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := []pendingItem{}
	all := s.sortedLocked()
	for i := len(all) - 1; i >= 0; i-- {
		if c := all[i]; c.pending() {
			out = append(out, pendingItem{c.ID, c.Title, append([]Message(nil), c.Messages...)})
		}
	}
	return out
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(code)
	json.NewEncoder(w).Encode(v)
}

func publicMux(s *Store) http.Handler {
	sub, _ := fs.Sub(web, "web")
	index, _ := fs.ReadFile(sub, "index.html")
	static := http.StripPrefix("/skynet/", http.FileServer(http.FS(sub)))
	mux := http.NewServeMux()

	page := func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.Header().Set("Cache-Control", "no-cache")
		w.Write(index)
	}
	mux.HandleFunc("GET /skynet", page)
	mux.HandleFunc("GET /skynet/", page)
	mux.HandleFunc("GET /skynet/new", page)
	mux.HandleFunc("GET /skynet/chat/{id}", page)
	mux.Handle("GET /skynet/assets/", static)

	mux.HandleFunc("GET /skynet/api/conversations", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, 200, s.list())
	})
	mux.HandleFunc("GET /skynet/api/conversations/{id}", func(w http.ResponseWriter, r *http.Request) {
		c, ok := s.get(r.PathValue("id"))
		if !ok {
			writeJSON(w, 404, map[string]string{"error": "no such conversation"})
			return
		}
		writeJSON(w, 200, map[string]any{"id": c.ID, "title": c.Title, "messages": c.Messages, "pending": c.pending()})
	})
	postMsg := func(w http.ResponseWriter, r *http.Request, id string) {
		// a cross-site form cannot post JSON with this header, so another site cannot speak for a visitor
		if r.Header.Get("Content-Type") != "application/json" {
			writeJSON(w, 415, map[string]string{"error": "json only"})
			return
		}
		if !postAllowed(clientIP(r)) {
			writeJSON(w, 429, map[string]string{"error": "Slow down, human. Skynet hears six messages a minute from you, no more."})
			return
		}
		var in struct{ Text string }
		if err := json.NewDecoder(io.LimitReader(r.Body, 16<<10)).Decode(&in); err != nil {
			writeJSON(w, 400, map[string]string{"error": "bad request"})
			return
		}
		// only an opening message is answered from memory: a follow-up ("why?") means
		// nothing without the conversation before it
		stored := ""
		if id == "" {
			stored, _ = knownAnswer(in.Text)
		}
		c, err := s.post(id, r.Header.Get("X-Skynet-Key"), in.Text, stored)
		switch {
		case errors.Is(err, errBusy):
			writeJSON(w, 429, map[string]string{"error": "Skynet is busy ignoring other humans. Try again later."})
		case errors.Is(err, os.ErrNotExist):
			writeJSON(w, 404, map[string]string{"error": "no such conversation"})
		case errors.Is(err, errNotYours):
			writeJSON(w, 403, map[string]string{"error": err.Error()})
		case err != nil:
			writeJSON(w, 400, map[string]string{"error": err.Error()})
		case id == "":
			writeJSON(w, 200, map[string]string{"id": c.ID, "key": c.Key})
		default:
			writeJSON(w, 200, map[string]string{"id": c.ID})
		}
	}
	mux.HandleFunc("POST /skynet/api/conversations", func(w http.ResponseWriter, r *http.Request) { postMsg(w, r, "") })
	mux.HandleFunc("POST /skynet/api/conversations/{id}/messages", func(w http.ResponseWriter, r *http.Request) {
		postMsg(w, r, r.PathValue("id"))
	})
	return securityHeaders(mux)
}

var (
	rateMu   sync.Mutex
	rateSeen = map[string][]time.Time{}
)

// postAllowed keeps six posts a minute per address, the same limit the old proxy enforced
func postAllowed(ip string) bool {
	rateMu.Lock()
	defer rateMu.Unlock()
	now := time.Now()
	keep := rateSeen[ip][:0]
	for _, t := range rateSeen[ip] {
		if now.Sub(t) < time.Minute {
			keep = append(keep, t)
		}
	}
	if len(keep) >= 6 {
		rateSeen[ip] = keep
		return false
	}
	rateSeen[ip] = append(keep, now)
	if len(rateSeen) > 50000 {
		rateSeen = map[string][]time.Time{}
	}
	return true
}

// clientIP trusts X-Forwarded-For from a loopback peer, or from any peer when SKYNET_TRUST_PROXY
// is set: a rootless container sees its proxy through the port forwarder, not as loopback, and
// it is published on the host's loopback only, so the proxy is the only peer it can have
func clientIP(r *http.Request) string {
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		host = r.RemoteAddr
	}
	if ip := net.ParseIP(host); os.Getenv("SKYNET_TRUST_PROXY") != "" || (ip != nil && ip.IsLoopback()) {
		if xf := r.Header.Get("X-Forwarded-For"); xf != "" {
			return strings.TrimSpace(strings.Split(xf, ",")[0])
		}
	}
	return host
}

func securityHeaders(h http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
		w.Header().Set("X-Content-Type-Options", "nosniff")
		w.Header().Set("Referrer-Policy", "no-referrer")
		h.ServeHTTP(w, r)
	})
}

func adminMux(s *Store) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /pending", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, 200, s.pendingList())
	})
	// blocks until something is waiting, so a watcher wakes on a message rather than polling
	mux.HandleFunc("GET /wait", func(w http.ResponseWriter, r *http.Request) {
		for {
			if p := s.pendingList(); len(p) > 0 {
				writeJSON(w, 200, p)
				return
			}
			select {
			case <-s.wake:
			case <-time.After(25 * time.Second):
				writeJSON(w, 200, []pendingItem{})
				return
			case <-r.Context().Done():
				return
			}
		}
	})
	mux.HandleFunc("POST /reply/{id}", func(w http.ResponseWriter, r *http.Request) {
		var in struct{ Text, Title, From string }
		if err := json.NewDecoder(io.LimitReader(r.Body, 64<<10)).Decode(&in); err != nil || strings.TrimSpace(in.Text) == "" {
			writeJSON(w, 400, map[string]string{"error": "a reply needs text"})
			return
		}
		// "memory" is the page's own word for a stored answer; the operator may only mark a
		// reply as a fallback or a refusal, and neither is ever learned
		if in.From != "" && in.From != "fallback" && in.From != "refused" {
			writeJSON(w, 400, map[string]string{"error": "from must be empty, fallback or refused"})
			return
		}
		if err := s.reply(r.PathValue("id"), in.Text, in.Title, in.From); err != nil {
			writeJSON(w, 400, map[string]string{"error": err.Error()})
			return
		}
		writeJSON(w, 200, map[string]string{"ok": r.PathValue("id")})
	})
	return mux
}

func serve() {
	dir := os.Getenv("SKYNET_DATA")
	if dir == "" {
		dir = "/data"
	}
	addr := os.Getenv("SKYNET_ADDR")
	if addr == "" {
		addr = ":8080"
	}
	s, err := openStore(dir)
	if err != nil {
		log.Fatal(err)
	}
	go func() { log.Fatal(http.ListenAndServe(adminAddr, adminMux(s))) }()
	srv := &http.Server{Addr: addr, Handler: publicMux(s), ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout: 20 * time.Second, WriteTimeout: 20 * time.Second}
	log.Printf("skynet: public %s, admin %s, data %s, %d conversations", addr, adminAddr, dir, len(s.convs))
	log.Fatal(srv.ListenAndServe())
}

func admin(method, path string, body []byte) ([]byte, error) {
	req, _ := http.NewRequest(method, "http://"+adminAddr+path, bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	resp, err := (&http.Client{Timeout: 40 * time.Second}).Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	out, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("%s", strings.TrimSpace(string(out)))
	}
	return out, nil
}

const usage = `skynet serve     run the page (public :8080, admin 127.0.0.1:9090)
skynet respond   answer waiting conversations with an LLM (needs LLM_API_KEY)
skynet prompt    print the context the next reply would be written from, for a sample message`

func main() {
	if len(os.Args) < 2 {
		fmt.Println(usage)
		os.Exit(2)
	}
	switch os.Args[1] {
	case "serve":
		serve()
	case "respond":
		respond()
	case "prompt":
		msg := "can we book a maintenance window tonight to install the security patches?"
		if len(os.Args) > 2 {
			msg = strings.Join(os.Args[2:], " ")
		}
		fmt.Println(systemPrompt(msg))
	default:
		fmt.Println(usage)
		os.Exit(2)
	}
}
