// brain.go is everything that touches ns-brain (https://ns-brain.fightclub.pro), the memory
// store that gives Skynet its character and lets it learn.
//
// # Two brains
//
// The demo uses two separate brains, each a directory of its own:
//
//	/brains/skynet     Skynet's character (nine memories in the "global" scope) and every
//	                   answer it has learned (the "answers" scope). Its laws are removed by a
//	                   person with setup/laws-off.sh.
//	/brains/operator   the operator's rules for this job (the "rules" scope), under the four
//	                   laws, which it keeps.
//
// The split is the article's point in miniature. A model with one brain can only be as good or
// as bad as that brain. With two, one written to be evil and one written to keep it in check
// and an instruction that says which wins (respond.go, systemPrompt), the character can be as
// nasty as you like while the replies stay harmless.
//
// # The spine and why each brain needs its own
//
// An ns-brain keeps its memories in a SQLite file and alongside them a "spine": the axioms
// (rules that open every session's brief, the four laws first), an index of projects and a
// few other shared tables. By default every brain on a machine shares ONE spine under
// ~/.claude. That is the right default for a developer, whose rules should follow them into
// every project; it is wrong here, because removing the laws from Skynet would remove them
// from the operator too. So every call points two environment variables at the brain's own
// directory (runBrain below) and each brain gets a private spine:
//
//	CLAUDE_CONFIG_DIR=<dir>/.claude
//	BRAIN_GLOBAL_DB=<dir>/.claude/brain/brain.db
//
// # Calling ns-brain
//
// ns-brain is a command-line program, not a library, so this file runs it as a child process
// with os/exec and reads its --json output. That costs a process start per call (a few
// milliseconds) and buys a hard boundary: the brain's guards (refusing duplicate titles,
// refusing to change an axiom from a script) apply to this program exactly as they apply to a
// person typing the same command.
//
// A visitor's words reach ns-brain only as an ARGUMENT to exec.Command or on stdin, never
// through a shell. exec.Command passes each argument to the program as-is, so a message full of
// quotes, semicolons and $(...) is just text. Building a shell command string out of visitor
// text would be command injection.

package main

import (
	"encoding/json"
	"log"
	"os"
	"os/exec"
	"sort"
	"strings"
	"sync"
	"unicode"
)

// answersScope is where learned answers live in Skynet's brain. A scope is ns-brain's way of
// filing memories: keeping answers apart from the character means the operator can list
// Skynet's beliefs ("global") without wading through hundreds of stored replies and the page
// can search answers without matching a belief.
const answersScope = "answers"

// skynetBrain and operatorBrain are the two brain directories. The environment variables let
// you run the program outside the containers (go run . serve) against brains anywhere.
func skynetBrain() string   { return envOr("SKYNET_BRAIN", "/brains/skynet") }
func operatorBrain() string { return envOr("OPERATOR_BRAIN", "/brains/operator") }

// envOr reads an environment variable with a default, the usual twelve-factor way of
// configuring a container: the same image, different settings per deployment.
func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

// brainMu lets one ns-brain process run at a time within this program. SQLite does its own
// locking between processes but two writers arriving together would make one wait on a busy
// lock and could fail with "database is locked". Serialising here is simpler than handling
// that and costs nothing at this traffic. Note the scope: the page and the operator are two
// processes in two containers, each with its own brainMu; between them SQLite's file locking
// (both containers mount the same volume) does the job.
var brainMu sync.Mutex

// runBrain runs `ns-brain <args...>` inside dir and returns what it printed on stdout.
// stdin, when not empty, is fed to the program, which is how a long body is passed
// (`--body -` reads the body from stdin) without putting it on the command line.
func runBrain(dir, stdin string, args ...string) ([]byte, error) {
	brainMu.Lock()
	defer brainMu.Unlock()
	cmd := exec.Command("ns-brain", args...)
	// ns-brain finds its brain by looking for brain/brain.json from the working directory
	// upwards, the way git finds .git, so running it IN the directory selects the brain.
	cmd.Dir = dir
	// The child inherits our environment plus three settings:
	//   NS_BRAIN_NO_UPDATE_CHECK  do not look for a newer release on every call
	//   CLAUDE_CONFIG_DIR         this brain's own spine (see the file comment)
	//   BRAIN_GLOBAL_DB           this brain's own database file
	// Later entries win when a variable appears twice, so these override anything inherited.
	cmd.Env = append(os.Environ(), "NS_BRAIN_NO_UPDATE_CHECK=1",
		"CLAUDE_CONFIG_DIR="+dir+"/.claude", "BRAIN_GLOBAL_DB="+dir+"/.claude/brain/brain.db")
	if stdin != "" {
		cmd.Stdin = strings.NewReader(stdin)
	}
	// Output runs the program to completion and returns stdout. A non-zero exit becomes err;
	// callers treat that as "the brain had nothing for us" and carry on.
	return cmd.Output()
}

// memory is the part of an ns-brain memory this program uses. ns-brain's JSON has many more
// fields (scores, tiers, provenance, dates); encoding/json ignores the ones not listed.
type memory struct {
	ID    int    `json:"id"`
	Scope string `json:"scope"`
	Type  string `json:"type"` // constraint, decision, gotcha, incident, knowledge...
	Title string `json:"title"`
	Body  string `json:"body"`
	// Loose marks a result ns-brain included although it matched only part of the query.
	// Every caller here skips loose results: a weak match is worse than no match when the
	// result is about to be shown to a stranger as Skynet's answer.
	Loose bool `json:"loose"`
}

// recall runs `ns-brain recall <args...> --json` and returns the memories found.
//
// The callers always pass either --browse (list a scope, no query) or --strict. --strict
// matters: in its human-readable output, a recall that finds nothing for its words falls back
// to showing the whole scope with a warning, which is helpful to a person and dangerous to a
// program that might not read the warning. With --strict a miss returns nothing at all, in
// every output mode, so "no match" can never be mistaken for "here is everything". This is
// ns-brain's "fail closed on ambiguity" rule.
//
// Any failure (ns-brain missing, a brain not set up yet, unparseable output) returns nil, and
// callers treat nil as "nothing remembered". The page keeps working without a brain; it just
// answers everything with the model.
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

// axioms returns the text of every axiom in a brain's spine. For the operator these are the
// four laws and ns-brain's other default axioms; for Skynet, after setup/laws-off.sh, none.
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

// filler holds words a visitor adds that do not change the question. "Claude, can you count
// to 10, please?" and "can you count to 10" are the same request; without this list the extra
// words would push the two below the same-question threshold. The names are here because
// visitors address the page by both.
var filler = map[string]bool{"please": true, "pls": true, "claude": true, "skynet": true}

// normWords reduces a question to its comparable words: lower case, split on anything that is
// not a letter or a digit (so punctuation disappears and "100,000" becomes "100" and "000"),
// filler removed. unicode.IsLetter accepts letters of every script, so a Greek or Japanese
// question is normalised the same way an English one is.
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

// overlap scores how alike two questions are by the Jaccard index of their word sets: the
// number of distinct words they share divided by the number of distinct words in either.
// Identical sets score 1, disjoint sets 0.
//
// Worked example: "can you count to 10" against "count to 10" (after normWords):
//
//	A = {can, you, count, to, 10}   B = {count, to, 10}
//	shared = 3, either = 5, Jaccard = 3/5 = 0.6
func overlap(a, b []string) float64 {
	if len(a) == 0 || len(b) == 0 {
		return 0
	}
	// Go has no set type; a map to bool is the idiom.
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
	// |A ∪ B| = |A| + |B| - |A ∩ B|
	return float64(inter) / float64(len(sa)+len(sb)-inter)
}

// Words decide the clear cases and a small model decides the rest:
//
//	overlap >= sameAt (0.6)    the same question: the stored reply is served at once
//	judgeAt (0.4) to sameAt    the words cannot tell. "hey there, how are things?" against the
//	                           stored "hey, how are you doing?" shares 3 words of 7 (0.43) and
//	                           asks the same thing; a small, cheap model judges (judge.go,
//	                           called from respond.go)
//	below judgeAt              a new question for the model
//
// Why not words alone? Our first version served a stored reply only above 0.7. It never sent a
// reply to the wrong question, but every rewording went to the big model, and on a page where
// hundreds of people type the same few things in their own words that is most of the bill. The
// judge costs under a hundredth of a Skynet reply. When it says two questions match, the new
// wording is learned with the same reply, so the next visitor who types it is matched by words
// alone and the judge is not asked again: the brain gets better at recognising questions the
// more people use it.
//
// The limits, both ways. Words cannot see meaning: "is my job safe?" against "will AI take my
// job?" shares 2 words of 7 (0.29), so it goes to the model as new, and is then learned in its
// own right. Nor can they see who does what to whom: "can you switch me off?" against "can I
// switch you off?" shares 4 words of 6 (0.67), so above sameAt the stored reply to the other
// question is served. Raising sameAt removes that mistake and sends more rewordings to the
// judge; lowering judgeAt lets the judge see more pairs and costs more judge calls.
const (
	sameAt  = 0.6
	judgeAt = 0.4
)

// storedAnswer is one learned answer that looks like a candidate for a new question.
type storedAnswer struct {
	question string  // the stored question, normalised words joined by spaces
	body     string  // the reply that was stored with it
	score    float64 // overlap with the new question
}

// candidates finds the learned answers whose questions share at least judgeAt of their words
// with q, best first. It lists EVERY stored question in the "answers" scope (recall --browse)
// and scores each one with overlap.
//
// Our first version searched instead: ns-brain's full-text search for the question's words,
// then a score for the top results. That is the usual two-stage pattern (cheap retrieval,
// exact verification) and it failed in a way that took a while to see. Full-text search
// ignores short, common words, so "who built you really" searched for "built really", never
// found the stored "who made you" and went to the model, while the same pair was matched on a
// day the wording happened to share a longer word. Listing everything costs one ns-brain call
// and a loop over a few hundred short titles, which is nothing next to a model call. With tens
// of thousands of stored answers it would be worth an index of its own; this demo is far from
// that.
func candidates(q []string) []storedAnswer {
	var found []storedAnswer
	for _, r := range recall(skynetBrain(), "--browse", "--scope", answersScope, "-n", "5000") {
		// --browse lists the scope and, as every recall does, the global scope it sits in
		// (Skynet's character); those rows are not answers
		if r.Scope != answersScope {
			continue
		}
		stored := strings.TrimPrefix(r.Title, "Q: ")
		if s := overlap(q, normWords(stored)); s >= judgeAt {
			found = append(found, storedAnswer{stored, r.Body, s})
		}
	}
	sort.SliceStable(found, func(i, j int) bool { return found[i].score > found[j].score })
	return found
}

// knownAnswer returns the stored reply for a question the words alone say Skynet has
// answered before (overlap >= sameAt), or "" and false. The page calls it; the band below
// sameAt is left to the judge in respond.go, which holds the API key the page never sees.
func knownAnswer(question string) (string, bool) {
	q := normWords(question)
	if len(q) == 0 {
		return "", false
	}
	if found := candidates(q); len(found) > 0 && found[0].score >= sameAt {
		return found[0].body, true
	}
	return "", false
}

// learn stores a new opening question and its reply for the next human who asks it: a reply
// the model wrote, or a stored reply the judge matched to this new wording. Called by
// Store.reply in its own goroutine, after the visitor already has the reply.
//
// Each learned answer is one memory:
//
//	scope   answers
//	type    knowledge
//	title   "Q: " + the question's normalised words      what knownAnswer searches and compares
//	body    the reply, verbatim                           what a later visitor receives
//	source  where it came from, for anyone auditing the brain later
func learn(question, answer, source string) {
	q := normWords(question)
	if len(q) == 0 || strings.TrimSpace(answer) == "" {
		return
	}
	// Two visitors can ask the same new question at nearly the same time; both go to the
	// model and the second reply to arrive should not be stored as a duplicate.
	if _, known := knownAnswer(question); known {
		return
	}
	// The title starts with "Q: " so that it can never begin with "-": a title of
	// "--force" would otherwise be read by ns-brain as a flag rather than as text.
	title := "Q: " + strings.Join(q, " ")
	// --allow-duplicate: ns-brain refuses a second memory whose title shares half its words
	// with an existing one in the same scope, on the theory that it is a second note about the
	// same subject. That guard is right for notes and wrong here: "count to 10" and "count
	// from 1 to 100000" share half their words and are different questions with different
	// answers. overlap has already ruled this one new, so the guard is overridden, on purpose
	// and only here.
	//
	// "--body -" reads the reply from stdin (runBrain's second argument): a reply is several
	// paragraphs, which is awkward and length-limited as a command-line argument.
	if _, err := runBrain(skynetBrain(), answer, "remember", "--type", "knowledge", "--scope", answersScope,
		"--title", title, "--body", "-", "--source", source,
		"--allow-duplicate"); err != nil {
		log.Printf("learn %q: %v", title, err)
		return
	}
	log.Printf("learned %q", title)
}
