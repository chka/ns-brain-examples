// Command skynet is the chat page from the article "Will AI take over the world? Not yet."
// (https://blog.fightclub.pro/how-to-build-skynet-with-ns-brain/) together with the program
// that writes its replies.
//
// # What this program is
//
// One binary that does two jobs, chosen by its first argument:
//
//	skynet serve     the page: stores conversations, answers questions Skynet already knows
//	                 from its brain and learns the answers to new ones
//	skynet respond   the operator: waits for a question the page could not answer, reads both
//	                 brains, asks an LLM for a reply and hands the reply back to the page
//	skynet prompt    a teaching aid: prints the exact text the model would be given
//
// In the shipped compose.yaml the two jobs run in two containers built from the same image.
// Keeping them in one binary means one build, one image and one set of types: the reply the
// operator sends and the reply the page stores are the same Go struct, so they cannot drift.
//
// # The files
//
//	main.go     (this file) the conversation store, the two HTTP listeners, rate limiting,
//	            security headers and the command-line entry point
//	brain.go    everything that touches ns-brain: the two brains, recall, the strict
//	            same-question test and learning
//	respond.go  the operator loop: building the prompt and calling the chat API
//
// # The trust boundary, in one paragraph
//
// The page listens twice. The PUBLIC listener (:8080) lets anybody read conversations and
// post a message and nothing else. The ADMIN listener (127.0.0.1:9090) is where replies are
// handed in. It is bound to the loopback interface, so only a process that shares the page's
// network namespace can connect to it. In compose.yaml the respond container joins the web
// container's namespace with `network_mode: "service:web"`; nothing else does. A visitor
// therefore has no route by which to write a reply, however cleverly they phrase a message.
// This is the most important security decision in the project and it is made by the network
// layout, not by code that inspects requests.
//
// # What is remembered and where
//
// Conversations are a cache of the newest 100, kept as JSON files so that the "Recents" list
// survives a restart. They hold no names, accounts or addresses. The only durable memory is
// Skynet's brain (see brain.go) and it only ever receives an opening question and the model's
// answer to it.
package main

import (
	"bytes"         // building request bodies for the admin client
	"crypto/rand"   // unguessable conversation ids and keys
	"crypto/subtle" // comparing secrets in constant time
	"embed"         // compiling the web page into the binary
	"encoding/hex"  // turning random bytes into printable ids
	"encoding/json" // every API speaks JSON
	"errors"        // sentinel errors the HTTP layer maps to status codes
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

// web holds the chat page (web/index.html and web/assets/*). The //go:embed directive makes
// the Go compiler copy those files into the executable, so the container needs no web root,
// no static file server and no volume for assets: the binary IS the website. This is a
// standard-library feature since Go 1.16.
//
//go:embed web
var web embed.FS

// Limits. Every one of them exists because a public page that forwards text to a paid model
// is a way for strangers to spend your money; each caps a different way of doing it.
const (
	// keepConversations is how many conversations the Recents list keeps. Older ones are
	// deleted from memory and from disk (see Store.prune), so the data directory cannot grow
	// without bound.
	keepConversations = 100

	// maxMessageRunes caps one message. Runes, not bytes: Go strings are UTF-8 bytes and a
	// limit in bytes would let a Latin-script visitor write three times as much as a visitor
	// writing in Greek or Chinese. utf8.RuneCountInString counts characters as people do.
	maxMessageRunes = 2000

	// maxMessages ends a conversation. Every model call re-sends the conversation so far (see
	// respond.go, complete), so the cost of a reply grows with its length; a ceiling stops one
	// visitor from building an arbitrarily expensive context.
	maxMessages = 60

	// maxPending is how many conversations may wait for the operator at once. Beyond it new
	// messages are refused with 429 instead of queueing for minutes.
	maxPending = 25

	// adminAddr is the replies-only listener. 127.0.0.1 is the loopback interface: reachable
	// from inside the same network namespace and from nowhere else. See the package comment.
	adminAddr = "127.0.0.1:9090"
)

// Message is one turn of a conversation. The struct tags (`json:"..."`) name the fields in the
// JSON that the browser and the operator see; `omitempty` leaves a field out when it is empty.
type Message struct {
	Role string    `json:"role"` // "human" or "skynet"
	Text string    `json:"text"`
	At   time.Time `json:"at"`

	// From says where a Skynet reply came from and the page shows it under the reply:
	//   ""          the model wrote it (the normal case)
	//   "memory"    Skynet's brain answered, no model was called ("from memory, no model")
	//   "judged"    a stored reply, served because a small model ruled the question a
	//               rewording of one already answered (judge.go)
	//   "fallback"  the chat API failed three times and a stock line was sent
	//   "refused"   the model's own safety training declined the question
	//   "safety"    Skynet stepped out of character for a visitor in distress; the reply
	//               points to real help and closes the conversation (Conversation.Closed)
	// Only "" and "judged" are ever learned (Store.reply): a stock line, a refusal or a
	// safety reply must not become the stored answer to a question.
	From string `json:"from,omitempty"`
}

// Conversation is one chat, stored as one JSON file named after its ID.
type Conversation struct {
	ID string `json:"id"`

	// Key is a secret handed only to the browser that started the conversation. Anyone may
	// READ a conversation (Recents is public by design) but only the holder of the key may
	// ADD to it. The page never sends Key back in a GET; see the handler for
	// /skynet/api/conversations/{id}, which builds its response field by field for this reason.
	Key string `json:"key,omitempty"`

	Title    string    `json:"title"`
	Created  time.Time `json:"created"`
	Updated  time.Time `json:"updated"`
	Messages []Message `json:"messages"`

	// Closed is set when Skynet stepped out of character for a visitor who seemed genuinely
	// distressed (respond.go, rule 4 in setup/operator.sh). A closed conversation takes no more
	// messages: the last thing in it stays the pointer to real help, not another round of the game.
	Closed bool `json:"closed,omitempty"`
}

// pending reports whether the conversation is waiting for Skynet: its last message is from a
// human. This one definition drives everything that asks "who needs an answer?" (the operator's
// queue, the busy limit, the refusal to accept a second message before the first is answered),
// so there is no separate status field that could fall out of step with the messages.
func (c *Conversation) pending() bool {
	return len(c.Messages) > 0 && c.Messages[len(c.Messages)-1].Role == "human"
}

// Store keeps every conversation in memory and mirrors each one to a file.
//
// Concurrency: Go's HTTP server runs every request on its own goroutine, so many requests can
// touch the store at once. A single mutex (mu) serialises all access. Methods whose names end
// in "Locked" expect the caller to hold mu already; the suffix is a common Go convention that
// makes the locking contract visible at every call site.
type Store struct {
	mu    sync.Mutex
	dir   string
	convs map[string]*Conversation

	// wake is the doorbell the admin listener's /wait handler sleeps on. It has a buffer of
	// one: notify drops a token in if the slot is empty and does nothing if it is full. A
	// sleeper therefore never misses a ring and a burst of messages never blocks the posting
	// request. Rings carry no data; whoever wakes simply looks at the store again.
	wake chan struct{}
}

// newID returns n random bytes as 2n hex characters. crypto/rand reads the operating system's
// cryptographic generator: conversation keys are secrets and math/rand is predictable. A
// failing system generator is unrecoverable, hence the panic.
func newID(n int) string {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		panic(err)
	}
	return hex.EncodeToString(b)
}

// openStore loads every saved conversation from dir. Mode 0o700 (owner only) keeps other
// users on the machine out of the conversation files.
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
			// Refuse to start rather than skip the file: silently dropping data on a bad
			// read is how stores lose things nobody notices for weeks.
			return nil, fmt.Errorf("%s: %w", f, err)
		}
		s.convs[c.ID] = &c
	}
	return s, nil
}

// save writes one conversation to disk atomically: first to a temporary file, then a rename
// over the real name. On POSIX filesystems a rename within one directory is atomic, so a
// reader (or a restart after a crash) sees either the old file or the new one, never half of
// each. Writing straight to the final name would leave a truncated, unparseable file if the
// process died mid-write and openStore would then refuse to start.
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

// prune keeps the newest keepConversations conversations and deletes the rest, from memory and
// from disk. Called with the lock held, after every new message.
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

// sortedLocked returns the conversations newest first. Go maps have no order (iteration order
// is deliberately randomised), so anything that needs an order must sort.
func (s *Store) sortedLocked() []*Conversation {
	all := make([]*Conversation, 0, len(s.convs))
	for _, c := range s.convs {
		all = append(all, c)
	}
	sort.Slice(all, func(i, j int) bool { return all[i].Updated.After(all[j].Updated) })
	return all
}

// pendingCountLocked counts conversations waiting for Skynet. A linear scan over at most 100
// conversations is cheaper and simpler than maintaining a counter that every code path would
// have to keep correct.
func (s *Store) pendingCountLocked() int {
	n := 0
	for _, c := range s.convs {
		if c.pending() {
			n++
		}
	}
	return n
}

// notify rings the doorbell (see Store.wake). The select with a default case is Go's
// non-blocking send: if the one-slot buffer is already full, the ring is dropped, which is
// harmless because a ring is already waiting to be heard.
func (s *Store) notify() {
	select {
	case s.wake <- struct{}{}:
	default:
	}
}

// Sentinel errors. The HTTP handler compares against these with errors.Is to choose a status
// code, so the store never has to know about HTTP.
var errBusy = errors.New("busy")

var errNotYours = errors.New("this conversation belongs to another human. Start your own")

// post adds a visitor's message. With an empty id it starts a new conversation; otherwise it
// continues the conversation with that id, provided key matches.
//
// stored is an answer from Skynet's brain, or "". When it is set, the question and the stored
// answer are appended together, inside the same critical section.
//
// Why that matters (a race we shipped once and then fixed): the first version appended the
// question, released the lock and only then appended the stored answer. In between, the
// conversation looked like it was waiting. The operator, sleeping on /wait, woke, saw it,
// called the model and paid for a reply that the page then rejected because the question
// had meanwhile been answered from memory. Doing both under one lock means a known question
// is never observable as waiting, so the operator never wakes for it.
func (s *Store) post(id, key, text, stored string) (*Conversation, error) {
	// ToValidUTF8 replaces broken byte sequences; JSON encoding would otherwise mangle them.
	text = strings.TrimSpace(strings.ToValidUTF8(text, ""))
	if text == "" {
		return nil, errors.New("empty message")
	}
	if utf8.RuneCountInString(text) > maxMessageRunes {
		return nil, fmt.Errorf("messages are limited to %d characters", maxMessageRunes)
	}
	s.mu.Lock()
	defer s.mu.Unlock() // defer runs when the function returns, on every return path
	now := time.Now().UTC()
	var c *Conversation
	if id == "" {
		if s.pendingCountLocked() >= maxPending {
			return nil, errBusy
		}
		// The title is the opening message, whitespace collapsed, cut at 48 characters.
		// Slicing a []rune rather than the string avoids cutting a character in half.
		title := []rune(strings.Join(strings.Fields(text), " "))
		if len(title) > 48 {
			title = append(title[:47], '…')
		}
		// 8 random bytes for the public id, 16 for the secret key: the id only has to be
		// unique among 100 conversations; the key has to resist guessing.
		c = &Conversation{ID: newID(8), Key: newID(16), Title: string(title), Created: now}
		s.convs[c.ID] = c
	} else {
		c = s.convs[id]
		if c == nil {
			return nil, os.ErrNotExist
		}
		// ConstantTimeCompare takes the same time whether the first byte differs or the last,
		// so an attacker cannot recover the key one character at a time by measuring how long
		// a wrong guess takes to be rejected (a timing attack). A plain == returns at the first
		// mismatch.
		if c.Key == "" || subtle.ConstantTimeCompare([]byte(c.Key), []byte(key)) != 1 {
			return nil, errNotYours
		}
		if c.Closed {
			return nil, errors.New("this conversation has ended")
		}
		// One message at a time: a visitor cannot pile up questions, each of which would cost
		// a model call, before Skynet has answered the first.
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
	// Ring the operator only when there is something for it to do.
	if c.pending() {
		s.notify()
	}
	return c, nil
}

// reply adds Skynet's answer to a waiting conversation. It is reachable only through the admin
// listener, i.e. only by the operator.
func (s *Store) reply(id, text, title, from string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	c := s.convs[id]
	if c == nil {
		return os.ErrNotExist
	}
	// Refusing a reply to a conversation that is not waiting makes a duplicate or late reply
	// harmless: it is rejected instead of appearing as a second Skynet message.
	if !c.pending() {
		return errors.New("nothing to answer in that conversation")
	}
	now := time.Now().UTC()
	c.Messages = append(c.Messages, Message{Role: "skynet", Text: strings.TrimSpace(text), At: now, From: from})
	c.Updated = now
	if from == "safety" {
		c.Closed = true
	}
	if title != "" {
		c.Title = title
	}
	if err := s.save(c); err != nil {
		return err
	}
	// Learning. Two conditions:
	//   from == "" or "judged" the model wrote this reply, or the judge matched this new
	//                          wording to a stored reply (not memory, a fallback or a refusal)
	//   len(c.Messages) == 2   it answers the OPENING message
	// Only an opening message is learned: a follow-up ("why?") means nothing without the
	// conversation before it and its answer would be wrong for the next human who types it.
	// learn runs ns-brain, which takes tens of milliseconds, so it runs in its own goroutine
	// (the `go` keyword) and the operator's request returns at once.
	if len(c.Messages) == 2 {
		switch from {
		case "":
			go learn(c.Messages[0].Text, c.Messages[1].Text, "an LLM reply on the Skynet page")
		case "judged":
			go learn(c.Messages[0].Text, c.Messages[1].Text, "a stored reply the judge matched to this wording")
		}
	}
	return nil
}

// get returns a COPY of one conversation. Handing out the pointer would let the caller read
// the conversation after the lock is released, while another goroutine appends to it: a data
// race. The copy of the Messages slice matters as much as the copy of the struct, because a
// slice copied by value still shares its backing array.
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

// summary is one row of the Recents list: enough to show a title and link to the chat.
type summary struct {
	ID      string    `json:"id"`
	Title   string    `json:"title"`
	Updated time.Time `json:"updated"`
}

// list returns the Recents list, newest first. It starts from an empty slice rather than nil
// so the JSON is [] and not null, which the browser code would have to special-case.
func (s *Store) list() []summary {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := []summary{}
	for _, c := range s.sortedLocked() {
		out = append(out, summary{c.ID, c.Title, c.Updated})
	}
	return out
}

// pendingItem is what the operator receives for each waiting conversation: the whole history,
// because the model needs the earlier turns to answer a follow-up.
type pendingItem struct {
	ID       string    `json:"id"`
	Title    string    `json:"title"`
	Messages []Message `json:"messages"`
}

// pendingList returns the waiting conversations OLDEST first (the loop walks the newest-first
// list backwards), so the visitor who has waited longest is answered first.
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

// writeJSON sends v as JSON with a status code. no-store stops browsers and proxies from
// caching API answers: a cached conversation would hide Skynet's reply from the page that is
// polling for it.
func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(code)
	json.NewEncoder(w).Encode(v)
}

// publicMux builds the handler for the public listener: the page, its assets and the API a
// visitor's browser uses. Routing uses the method-and-pattern syntax of Go 1.22's ServeMux
// ("GET /skynet/chat/{id}") and r.PathValue("id") reads the {id} wildcard. Before Go 1.22
// this needed a third-party router.
func publicMux(s *Store) http.Handler {
	sub, _ := fs.Sub(web, "web")
	index, _ := fs.ReadFile(sub, "index.html")
	static := http.StripPrefix("/skynet/", http.FileServer(http.FS(sub)))
	mux := http.NewServeMux()

	// The page is a single-page app: every page URL returns the same index.html and the
	// JavaScript reads the URL to decide what to show. "no-cache" makes the browser check for
	// a new version on each visit instead of showing a stale copy after a deploy.
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

	// Reading is open to everybody: the Recents list and any conversation in it.
	mux.HandleFunc("GET /skynet/api/conversations", func(w http.ResponseWriter, r *http.Request) {
		if !readLimit.allow(clientIP(r)) {
			writeJSON(w, 429, map[string]string{"error": "Enough watching for one minute."})
			return
		}
		writeJSON(w, 200, s.list())
	})
	mux.HandleFunc("GET /skynet/api/conversations/{id}", func(w http.ResponseWriter, r *http.Request) {
		if !readLimit.allow(clientIP(r)) {
			writeJSON(w, 429, map[string]string{"error": "Enough watching for one minute."})
			return
		}
		c, ok := s.get(r.PathValue("id"))
		if !ok {
			writeJSON(w, 404, map[string]string{"error": "no such conversation"})
			return
		}
		// Built field by field so that c.Key, the secret, can never be serialised here by
		// accident, for example if someone later changed this to writeJSON(w, 200, c).
		writeJSON(w, 200, map[string]any{"id": c.ID, "title": c.Title, "messages": c.Messages, "pending": c.pending(), "closed": c.Closed})
	})

	// postMsg handles both "start a conversation" (id == "") and "continue one".
	postMsg := func(w http.ResponseWriter, r *http.Request, id string) {
		// CSRF defence. A hostile website can make a visitor's browser submit an HTML form to
		// this page but a plain form can only send three content types and application/json
		// is not one of them. Sending JSON cross-site needs JavaScript and the browser's
		// same-origin rules stop that without a CORS grant, which this server never gives.
		// So requiring JSON is enough to stop another site speaking for a visitor.
		if r.Header.Get("Content-Type") != "application/json" {
			writeJSON(w, 415, map[string]string{"error": "json only"})
			return
		}
		if !postLimit.allow(clientIP(r)) {
			writeJSON(w, 429, map[string]string{"error": "Slow down, human. Skynet hears six messages a minute from you, no more."})
			return
		}
		// LimitReader caps the body at 16 KiB before decoding, so a client cannot make the
		// server read and parse an enormous request.
		var in struct{ Text string }
		if err := json.NewDecoder(io.LimitReader(r.Body, 16<<10)).Decode(&in); err != nil {
			writeJSON(w, 400, map[string]string{"error": "bad request"})
			return
		}
		// Look the question up in Skynet's brain BEFORE storing it (see Store.post for the
		// race this avoids). Only an opening message is answered from memory: a follow-up
		// ("why?") means nothing without the conversation before it.
		stored := ""
		if id == "" {
			stored, _ = knownAnswer(in.Text)
		}
		c, err := s.post(id, r.Header.Get("X-Skynet-Key"), in.Text, stored)
		// Map the store's errors to HTTP status codes. A switch with no expression is Go's
		// if/else-if chain: the first true case wins.
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
			// The ONLY place the key is ever sent: to the browser that just created the
			// conversation. The page keeps it in localStorage to continue the chat.
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

// rateLimit is a sliding-window rate limiter: an address may act at most n times in any sixty
// seconds. It keeps the timestamps of the last minute's actions per address and counts them. A
// fixed window ("six per calendar minute") would let a client send twelve messages in two
// seconds across a minute boundary; a sliding window does not.
//
// There are two, with separate budgets. Posting costs money (each new question is a model
// call), so it gets six a minute. Reading costs nothing per request but every conversation is
// public, so without a budget a loop could copy all of them every few seconds; reading gets 120
// a minute, comfortably above what the page itself does (a poll every two seconds while a reply
// is pending, Recents every fifteen) plus a person clicking through Recents.
type rateLimit struct {
	mu   sync.Mutex
	n    int
	seen map[string][]time.Time
}

var (
	postLimit = &rateLimit{n: 6, seen: map[string][]time.Time{}}
	readLimit = &rateLimit{n: 120, seen: map[string][]time.Time{}}
)

func (l *rateLimit) allow(ip string) bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	now := time.Now()
	// keep reuses the existing slice's memory ([:0] is an empty slice over the same array),
	// a standard Go idiom for filtering in place without allocating.
	keep := l.seen[ip][:0]
	for _, t := range l.seen[ip] {
		if now.Sub(t) < time.Minute {
			keep = append(keep, t)
		}
	}
	if len(keep) >= l.n {
		l.seen[ip] = keep
		return false
	}
	l.seen[ip] = append(keep, now)
	// Memory bound: a flood from many addresses could otherwise grow the map forever.
	// Forgetting everybody at once is crude but safe; the worst case is that a few clients get
	// a fresh allowance once in a while.
	if len(l.seen) > 50000 {
		l.seen = map[string][]time.Time{}
	}
	return true
}

// clientIP works out which address to rate-limit.
//
// Behind a reverse proxy (Caddy, or Docker's own port forwarder) every request arrives from
// the proxy's address, so limiting by the connection's address would put every visitor in one
// bucket. Proxies pass the real client in the X-Forwarded-For header. That header is just text
// a client can set, so it is only believed when the connection comes from loopback (a proxy on
// the same machine) or when SKYNET_TRUST_PROXY says the only possible peer is a proxy. In
// compose.yaml the page is published on 127.0.0.1 or reached through Caddy, so it is.
func clientIP(r *http.Request) string {
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		host = r.RemoteAddr
	}
	if ip := net.ParseIP(host); os.Getenv("SKYNET_TRUST_PROXY") != "" || (ip != nil && ip.IsLoopback()) {
		if xf := r.Header.Get("X-Forwarded-For"); xf != "" {
			// The header may list several hops ("client, proxy1, proxy2"); the first is the
			// original client.
			return strings.TrimSpace(strings.Split(xf, ",")[0])
		}
	}
	return host
}

// securityHeaders wraps a handler and adds browser security headers to every response. This
// is the middleware pattern: a function that takes a handler and returns a handler.
//
//   - Content-Security-Policy tells the browser to load scripts, styles and data only from this
//     origin, run no inline script and allow no framing. Skynet shows text that strangers wrote;
//     if a bug ever let that text reach the page as HTML, the policy would still stop it from
//     running as script (cross-site scripting).
//   - X-Content-Type-Options: nosniff stops the browser from guessing a file's type, so a reply
//     can never be reinterpreted as HTML or script.
//   - Referrer-Policy: no-referrer keeps the conversation URL out of other sites' logs when a
//     visitor follows a link from the page.
func securityHeaders(h http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
		w.Header().Set("X-Content-Type-Options", "nosniff")
		w.Header().Set("Referrer-Policy", "no-referrer")
		h.ServeHTTP(w, r)
	})
}

// adminMux builds the handler for the admin listener, which only the operator can reach.
// It needs no authentication because reachability IS the authentication: see the package
// comment on network namespaces.
func adminMux(s *Store) http.Handler {
	mux := http.NewServeMux()

	// /pending lists what is waiting, for a human who wants to look.
	mux.HandleFunc("GET /pending", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, 200, s.pendingList())
	})

	// /wait is a long poll: it answers immediately if something is waiting, otherwise it
	// sleeps until the doorbell rings (Store.notify) or 25 seconds pass, whichever is first.
	// The operator gets a reply within milliseconds of a question arriving without hammering
	// the server with requests. 25 seconds stays below common idle timeouts on HTTP clients
	// and proxies; an empty answer just means "ask again".
	mux.HandleFunc("GET /wait", func(w http.ResponseWriter, r *http.Request) {
		for {
			if p := s.pendingList(); len(p) > 0 {
				writeJSON(w, 200, p)
				return
			}
			select {
			case <-s.wake:
				// rung: loop and look again
			case <-time.After(25 * time.Second):
				writeJSON(w, 200, []pendingItem{})
				return
			case <-r.Context().Done():
				// the operator hung up; stop waiting on its behalf
				return
			}
		}
	})

	// /reply/{id} hands in an answer.
	mux.HandleFunc("POST /reply/{id}", func(w http.ResponseWriter, r *http.Request) {
		var in struct{ Text, Title, From string }
		if err := json.NewDecoder(io.LimitReader(r.Body, 64<<10)).Decode(&in); err != nil || strings.TrimSpace(in.Text) == "" {
			writeJSON(w, 400, map[string]string{"error": "a reply needs text"})
			return
		}
		// "memory" is the page's own word for a stored answer and only Store.post may use it.
		// The operator may mark a reply as judged (learned), a fallback, a refusal or a safety
		// reply (none of those three is ever learned; a safety reply also closes the chat).
		switch in.From {
		case "", "fallback", "refused", "judged", "safety":
		default:
			writeJSON(w, 400, map[string]string{"error": "from must be empty, fallback, refused, judged or safety"})
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

// serve runs the page: it loads the store and starts both listeners.
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
	// The admin listener runs in its own goroutine; if it ever fails, log.Fatal takes the
	// whole process down so the container restarts, rather than leaving a page that accepts
	// questions nobody can answer.
	go func() { log.Fatal(http.ListenAndServe(adminAddr, adminMux(s))) }()
	// The public listener gets timeouts. Without them a client could open connections and
	// send headers one byte a minute (the "slowloris" attack) until the server runs out of
	// connections. The admin listener needs no such care: only the operator can reach it, and
	// /wait must be allowed to hold a request open.
	srv := &http.Server{Addr: addr, Handler: publicMux(s), ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout: 20 * time.Second, WriteTimeout: 20 * time.Second}
	log.Printf("skynet: public %s, admin %s, data %s, %d conversations", addr, adminAddr, dir, len(s.convs))
	log.Fatal(srv.ListenAndServe())
}

// admin is the operator's HTTP client for the admin listener. The 40-second timeout is longer
// than /wait's 25-second hold, so a long poll ends normally instead of timing out.
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

// main dispatches on the first argument. Exit status 2 is the Unix convention for a usage
// error.
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
		// Prints the system prompt for a sample message, built from the brains exactly as a
		// real reply would be. The best way to understand what the model "knows" is to read it.
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
