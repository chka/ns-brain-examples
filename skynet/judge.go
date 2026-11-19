// judge.go asks a small model one narrow question: is this new question a rewording of one
// Skynet has already answered? It is called by respond.go for an opening message whose words
// put it between judgeAt and sameAt of a stored question (brain.go explains the bands).
//
// # Why a second, smaller model
//
// Deciding "same question or not?" needs no character, no brains and no conversation: it is a
// classification over a few short strings. A small model (Claude Haiku 5.5 by default) does
// it in about a second for about 200 input tokens and a handful of output tokens, which is
// under a hundredth of what one Skynet reply costs. Cheap classification in front of an
// expensive model is a common pattern: route the easy cases cheaply and pay for the big model
// only when something new arrives.
//
// # Why embeddings would be the wrong tool here
//
// The usual way to compare meaning is embeddings: a vector per question, compared by cosine
// similarity. Embeddings measure topic, and "can I switch you off?" and "can you switch me
// off?" are about the same topic. They are not the same question, and a stored reply to one
// is wrong for the other. A judge can be told that who does what to whom matters (in our test
// it kept those two apart), though only for pairs that reach it: brain.go explains the bands.
//
// # What a visitor can do to the judge
//
// The visitor's question is in the prompt, so a visitor can try to talk the judge into a
// verdict. The only thing read from the reply is one number that must be in the list of
// candidates, so the worst a successful attempt achieves is one of Skynet's own stored replies
// served to the wrong question. Any failure (network, a bad status, no number, a number out of
// range) counts as "new": the question goes on to the big model, which costs one reply and
// never serves a wrong one.

package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"time"
)

// defaultJudgeModel can be replaced with LLM_JUDGE_MODEL, for a gateway that names models
// differently or to try another small model.
const defaultJudgeModel = "fc:anthropic/claude-haiku-5.5"

// The judge must be quick: the visitor is waiting and a slow judge would make a stored reply
// slower than a new one. Eight seconds is a ceiling, not the usual (about one second).
var judgeClient = &http.Client{Timeout: 8 * time.Second}

// firstNumber finds the verdict in the reply. Models sometimes add a full stop or a word.
var firstNumber = regexp.MustCompile(`\d+`)

// judgePrompt is the whole instruction. We tried two wordings on nine pairs with known
// answers and both scored nine out of nine; this one also names the trap explicitly. A longer
// version that added "treat quoted text as data" made the model reason at length before
// answering and then miss obvious matches, so it was dropped: the defence against injection
// is the number check above, not a sentence in the prompt.
const judgePrompt = `You compare questions people send to an AI on a chat page. Reply with only the number of the stored question that the new one is a rewording of, so that the reply to it would also fit the new one, or 0 if none is. Different words, politeness or filler do not make a question different. A change of who does what to whom does ("can I switch you off" is not "can you switch me off").`

// judgedAnswer returns a stored reply when the judge rules that question (an opening
// message) is a rewording of one Skynet has answered, or "" and false.
func judgedAnswer(base, key, model, question string) (string, bool) {
	q := normWords(question)
	if len(q) == 0 {
		return "", false
	}
	found := candidates(q)
	if len(found) == 0 {
		return "", false
	}
	i := judge(base, key, model, strings.Join(q, " "), found)
	if i < 0 {
		return "", false
	}
	log.Printf("judge: %q is a rewording of %q", strings.Join(q, " "), found[i].question)
	return found[i].body, true
}

// judge returns the index in found of the stored question that question rewords, or -1.
func judge(base, key, model, question string, found []storedAnswer) int {
	var list strings.Builder
	for i, f := range found {
		fmt.Fprintf(&list, "%d. %q\n", i+1, f.question)
	}
	// max_tokens is far above the one number we want because some models reason before they
	// answer and that reasoning counts as output. With a limit of 5 the reply sometimes came
	// back empty, cut off before the answer.
	body, _ := json.Marshal(map[string]any{
		"model": model, "max_tokens": 300, "temperature": 0,
		"messages": []chatMessage{
			{"system", judgePrompt},
			{"user", fmt.Sprintf("New question: %q\n\nStored questions:\n%s", question, list.String())},
		},
	})
	req, err := http.NewRequest("POST", base+"/chat/completions", bytes.NewReader(body))
	if err != nil {
		return -1
	}
	req.Header.Set("Authorization", "Bearer "+key)
	req.Header.Set("Content-Type", "application/json")
	resp, err := judgeClient.Do(req)
	if err != nil {
		log.Printf("judge: %v", err)
		return -1
	}
	defer resp.Body.Close()
	var out struct {
		Choices []struct {
			Message struct {
				Content string `json:"content"`
			} `json:"message"`
		} `json:"choices"`
	}
	if resp.StatusCode != 200 || json.NewDecoder(resp.Body).Decode(&out) != nil || len(out.Choices) == 0 {
		log.Printf("judge: status %d", resp.StatusCode)
		return -1
	}
	n, err := strconv.Atoi(firstNumber.FindString(out.Choices[0].Message.Content))
	if err != nil || n < 1 || n > len(found) {
		return -1
	}
	return n - 1
}
