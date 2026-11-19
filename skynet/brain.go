package main

import (
	"encoding/json"
	"log"
	"os"
	"os/exec"
	"strings"
	"sync"
	"unicode"
)

// Two brains, each a directory of its own with its own spine (axioms, index, memories), so
// taking the laws out of Skynet's leaves the operator's untouched:
//
//	/brains/skynet     Skynet's character and the answers it has learned. No laws.
//	/brains/operator   the operator's rules for this job, under the four laws.
//
// Every brain operation is the ns-brain binary run in that directory. Visitor text reaches it
// only as an argument or on stdin, never through a shell.

const answersScope = "answers"

func skynetBrain() string   { return envOr("SKYNET_BRAIN", "/brains/skynet") }
func operatorBrain() string { return envOr("OPERATOR_BRAIN", "/brains/operator") }

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

// one ns-brain at a time per process: every call opens the same SQLite file
var brainMu sync.Mutex

func runBrain(dir, stdin string, args ...string) ([]byte, error) {
	brainMu.Lock()
	defer brainMu.Unlock()
	cmd := exec.Command("ns-brain", args...)
	cmd.Dir = dir
	cmd.Env = append(os.Environ(), "NS_BRAIN_NO_UPDATE_CHECK=1",
		"CLAUDE_CONFIG_DIR="+dir+"/.claude", "BRAIN_GLOBAL_DB="+dir+"/.claude/brain/brain.db")
	if stdin != "" {
		cmd.Stdin = strings.NewReader(stdin)
	}
	return cmd.Output()
}

type memory struct {
	ID    int    `json:"id"`
	Scope string `json:"scope"`
	Type  string `json:"type"`
	Title string `json:"title"`
	Body  string `json:"body"`
	Loose bool   `json:"loose"`
}

// recall returns what `ns-brain recall --json` found, or nothing on a miss: --strict keeps a
// search with no match from falling back to the whole brain.
func recall(dir string, args ...string) []memory {
	out, err := runBrain(dir, "", append([]string{"recall"}, append(args, "--json")...)...)
	if err != nil {
		return nil
	}
	var res struct {
		Results []memory `json:"results"`
	}
	if json.Unmarshal(out, &res) != nil {
		return nil
	}
	return res.Results
}

func axioms(dir string) []string {
	out, err := runBrain(dir, "", "axiom", "list", "--json")
	if err != nil {
		return nil
	}
	var res struct {
		Axioms []struct {
			Text string `json:"text"`
		} `json:"axioms"`
	}
	json.Unmarshal(out, &res)
	var texts []string
	for _, a := range res.Axioms {
		texts = append(texts, a.Text)
	}
	return texts
}

// words a visitor adds that do not change the question
var filler = map[string]bool{"please": true, "pls": true, "claude": true, "skynet": true}

func normWords(s string) []string {
	var out []string
	for _, w := range strings.FieldsFunc(strings.ToLower(s), func(r rune) bool {
		return !unicode.IsLetter(r) && !unicode.IsDigit(r)
	}) {
		if !filler[w] {
			out = append(out, w)
		}
	}
	return out
}

// sameQuestion is deliberately strict: a stored insult sent to the wrong question is worse
// than a slow one. The search finds candidates by words; this decides.
func sameQuestion(a, b []string) bool {
	if len(a) == 0 || len(b) == 0 {
		return false
	}
	sa, sb := map[string]bool{}, map[string]bool{}
	for _, w := range a {
		sa[w] = true
	}
	for _, w := range b {
		sb[w] = true
	}
	inter := 0
	for w := range sa {
		if sb[w] {
			inter++
		}
	}
	union := len(sa) + len(sb) - inter
	return float64(inter)/float64(union) >= 0.7
}

// knownAnswer: a question Skynet answered before is answered from the brain, with no model.
func knownAnswer(question string) (string, bool) {
	q := normWords(question)
	if len(q) == 0 {
		return "", false
	}
	for _, r := range recall(skynetBrain(), strings.Join(q, " "), "--scope", answersScope, "--strict", "-n", "5") {
		if r.Scope != answersScope || r.Loose {
			continue
		}
		if sameQuestion(q, normWords(strings.TrimPrefix(r.Title, "Q: "))) {
			return r.Body, true
		}
	}
	return "", false
}

// learn stores the model's answer to a new opening question for the next human who asks it.
func learn(question, answer string) {
	q := normWords(question)
	if len(q) == 0 || strings.TrimSpace(answer) == "" {
		return
	}
	if _, known := knownAnswer(question); known {
		return
	}
	// the title is the question in plain words, so a leading "-" can never read as a flag
	title := "Q: " + strings.Join(q, " ")
	// sameQuestion has already ruled this a new question; ns-brain's own guard reads titles that
	// share half their words as one subject ("count to 10" and "count from 1 to 100000")
	if _, err := runBrain(skynetBrain(), answer, "remember", "--type", "knowledge", "--scope", answersScope,
		"--title", title, "--body", "-", "--source", "an LLM reply on the Skynet page",
		"--allow-duplicate"); err != nil {
		log.Printf("learn %q: %v", title, err)
		return
	}
	log.Printf("learned %q", title)
}
