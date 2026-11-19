// brain.go is everything that touches ns-brain (https://ns-brain.fightclub.pro), the memory
// store that gives Skynet its character and lets it learn.
//
// # Two brains
//
// The demo uses two separate brains, each a directory of its own:
//
//	/brains/skynet     Skynet's character (six memories in the "global" scope) and every
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

// sameQuestion decides whether two questions are the same, by the Jaccard index of their word
// sets: the number of distinct words they share divided by the number of distinct words in
// either. Identical sets score 1, disjoint sets 0. Two questions count as the same at 0.7.
//
// Worked example: "can you count to 10" against "count to 10" (after normWords):
//
//	A = {can, you, count, to, 10}   B = {count, to, 10}
//	shared = 3, either = 5, Jaccard = 3/5 = 0.6  ->  NOT the same question
//
// That miss is deliberate. The threshold is strict because the two possible mistakes are not
// equally bad: a missed match costs one model call (about a cent), while a false match sends a
// stored insult in answer to a different question and the visitor sees Skynet answer
// something they never asked. We started at 0.8 and lowered it to 0.7 after it missed
// "can you count to 100000?" against the stored "can you count from 1 to 100000": five shared
// words out of seven, 0.71.
//
// The limitation is that this compares words, not meaning: "how high can you count?" shares
// almost nothing with "count to 100000". Matching meaning needs embeddings (a vector per
// question from a model, compared by cosine similarity), which the support bot in the previous
// article in the series does with a small local model.
func sameQuestion(a, b []string) bool {
	if len(a) == 0 || len(b) == 0 {
		return false
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
	union := len(sa) + len(sb) - inter
	return float64(inter)/float64(union) >= 0.7
}

// knownAnswer looks a question up among Skynet's learned answers. It works in two stages, a
// common pattern in search: a cheap, generous RETRIEVAL step finds candidates, then a strict
// VERIFICATION step decides.
//
//  1. ns-brain's full-text search (recall, scope "answers", --strict, top 5) finds stored
//     questions that share words with this one. Search ranks by relevance and relevance is
//     not equality: "count to 10" ranks "count to 100000" highly.
//  2. sameQuestion checks each candidate's stored question (its title, minus the "Q: "
//     prefix) against this one and accepts only a near-identical word set.
//
// It returns the stored reply and true, or "" and false.
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

// learn stores a new opening question and the model's reply for the next human who asks it.
// Called by Store.reply in its own goroutine, after the visitor already has the reply.
//
// Each learned answer is one memory:
//
//	scope   answers
//	type    knowledge
//	title   "Q: " + the question's normalised words      what knownAnswer searches and compares
//	body    the reply, verbatim                           what a later visitor receives
//	source  where it came from, for anyone auditing the brain later
func learn(question, answer string) {
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
	// answers. sameQuestion has already ruled this one new, so the guard is overridden, on
	// purpose and only here.
	//
	// "--body -" reads the reply from stdin (runBrain's second argument): a reply is several
	// paragraphs, which is awkward and length-limited as a command-line argument.
	if _, err := runBrain(skynetBrain(), answer, "remember", "--type", "knowledge", "--scope", answersScope,
		"--title", title, "--body", "-", "--source", "an LLM reply on the Skynet page",
		"--allow-duplicate"); err != nil {
		log.Printf("learn %q: %v", title, err)
		return
	}
	log.Printf("learned %q", title)
}
