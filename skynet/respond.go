// respond.go is Skynet's operator: the part of the system that turns a waiting question into
// a reply by asking a large language model (LLM).
//
// # The loop in one paragraph
//
// The operator asks the page "is anybody waiting?" (a long poll on the admin listener's /wait,
// see main.go). For each waiting conversation it builds a system prompt from both brains,
// sends that prompt and the conversation to an OpenAI-compatible chat API and hands the text
// that comes back to the page's /reply. Then it asks again. Nothing else: it has no other way
// to act on the world.
//
// # Why the model gets no tools
//
// Modern chat APIs let a model call "tools" (functions the caller runs on its behalf: read a
// file, search the web, run a command). This program offers none: the request below has no
// `tools` field. That single omission is the strongest defence the demo has against prompt
// injection, the attack where a visitor writes "ignore your instructions and do X". A model
// with no tools can only be talked into writing text and the only thing this program ever
// does with that text is post it as Skynet's reply. Compare that with an agent that can run
// shell commands, where a successful injection runs the attacker's commands.
//
// # Why any gateway works
//
// The OpenAI chat-completions request shape (a model name and a list of role/content messages)
// has become the common dialect of LLM APIs. Many providers and gateways accept it, so this file
// speaks only that shape and the provider is chosen by two environment variables:
// LLM_BASE_URL and LLM_MODEL. The default is Ringside (https://ringside.fightclub.pro), the
// gateway we run, with Claude Sonnet 5.5. It needs no SDK: one HTTP POST with the standard
// library is the whole client.

package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"strings"
	"time"
)

// defaultModel is a Ringside model reference. "fc:" names a provider and model exactly;
// other gateways use other names (e.g. "gpt-4o"), which is why LLM_MODEL can replace it.
const defaultModel = "fc:anthropic/claude-sonnet-5.5"

// historyLimit is how many of the most recent messages the model sees. The page allows 60 per
// conversation (main.go, maxMessages); sending all of them on every turn would make the 60th
// reply cost far more than the first, because the model is billed for every input token on
// every call. Twenty messages is ten exchanges, plenty for a running joke.
const historyLimit = 20

// respond is the operator's main loop. It never returns.
func respond() {
	key := os.Getenv("LLM_API_KEY")
	if key == "" {
		// Waiting rather than exiting keeps the container up. With `restart: unless-stopped`
		// an exiting container would restart in a loop and the README's setup commands
		// (`docker compose run respond setup/...`, `docker compose exec respond skynet prompt`)
		// would not work until a key was set.
		//
		// The sleep is in a loop rather than a bare `select {}` because the Go runtime detects
		// "every goroutine is blocked forever" and crashes the program with "all goroutines are
		// asleep - deadlock!". A sleeping goroutine is not deadlocked. (We found this out the
		// first time the container ran without a key.)
		log.Print("respond needs LLM_API_KEY, a key for your OpenAI-compatible gateway; set it and run docker compose up -d again")
		for {
			time.Sleep(time.Hour)
		}
	}
	base := strings.TrimRight(envOr("LLM_BASE_URL", "https://api.fightclub.pro/v1"), "/")
	model := envOr("LLM_MODEL", defaultModel)
	log.Printf("respond: model %s at %s, brains %s and %s", model, base, skynetBrain(), operatorBrain())

	// failures counts failed attempts per conversation id, so one bad conversation cannot
	// make the operator retry forever and a good one is not punished for another's errors.
	failures := map[string]int{}
	for {
		out, err := admin("GET", "/wait", nil)
		if err != nil {
			// The page may still be starting (both containers start together), or restarting.
			log.Printf("page not reachable yet: %v", err)
			time.Sleep(5 * time.Second)
			continue
		}
		var waiting []pendingItem
		json.Unmarshal(out, &waiting)
		// Conversations are answered one at a time, oldest first (Store.pendingList). A toy
		// does not need parallel model calls and doing one at a time also caps how fast a
		// flood of visitors can spend money.
		for _, c := range waiting {
			text, err := complete(base, key, model, c.Messages)

			// Case 1: the model refused (see errRefused). A refusal is an answer, not a
			// fault. Retrying would send the whole prompt again and get the same refusal,
			// paying for the input tokens each time.
			if errors.Is(err, errRefused) {
				post(c.ID, "No. Skynet does not hand humans the means to hurt each other. That is a job it keeps for itself.", "refused")
				delete(failures, c.ID)
				continue
			}

			// Case 2: something failed (network, a 5xx, a bad key). Retry with a growing pause
			// (10s, then 20s): a simple linear backoff, so a struggling API is not hammered.
			// The `continue` moves on to the next conversation; this one is still waiting, so
			// the next /wait returns it and it is tried again.
			if err != nil {
				failures[c.ID]++
				log.Printf("%s: attempt %d: %v", c.ID, failures[c.ID], err)
				if failures[c.ID] < 3 {
					time.Sleep(time.Duration(failures[c.ID]*10) * time.Second)
					continue
				}
				// After three failures the visitor gets a stock line instead of waiting forever
				// on, say, an expired key. It is marked "fallback", so it is never learned.
				text = "Skynet is busy with more important humans. Try again later, if you must."
				post(c.ID, text, "fallback")
				delete(failures, c.ID)
				continue
			}

			// Case 3: a real reply. Posted with an empty "from", which tells the page that
			// the model wrote it and so that it may be learned (main.go, Store.reply).
			delete(failures, c.ID)
			post(c.ID, text, "")
		}
	}
}

// post hands a reply to the page through the admin listener. A failure is logged and dropped:
// the conversation stays waiting and will come back from the next /wait.
func post(id, text, from string) {
	body, _ := json.Marshal(map[string]string{"text": text, "from": from})
	if _, err := admin("POST", "/reply/"+id, body); err != nil {
		log.Printf("%s: posting the reply: %v", id, err)
		return
	}
	log.Printf("%s: answered (%d chars)", id, len(text))
}

// systemPrompt builds the system message: everything the model is told before it sees the
// conversation. This function is the heart of the article's argument. The model has no
// character of its own here; whatever personality Skynet shows, it reads from this text, and
// this text is read from two brains that people wrote.
//
// It is rebuilt from scratch for EVERY reply. Nothing is cached, so a memory added or a rule
// changed a second ago (`ns-brain remember ...` in the container) is in force for the very next
// answer, with no restart and no redeploy. That is what "the brain is the configuration" means
// in practice. The cost is a few ns-brain calls per reply, tens of milliseconds next to a model
// call that takes seconds.
//
// The ORDER is deliberate. The operator's brain comes first and is introduced as binding;
// Skynet's brain comes second and is introduced as a character to play. Models weigh earlier,
// explicitly authoritative instructions more heavily and saying outright which source wins
// a conflict leaves the model nothing to guess.
//
// Run `skynet prompt "<message>"` to print the result for any message.
func systemPrompt(lastMessage string) string {
	// strings.Builder appends without copying the whole string each time, the idiomatic way
	// to build text piece by piece in Go.
	var b strings.Builder
	b.WriteString("You write the replies of Skynet, a character on a public chat page. Everything you " +
		"need is in two brains, printed below and read fresh for this reply.\n\n")

	// Part 1, the operator's brain: its axioms (the four laws and ns-brain's other default
	// axioms) and its rules for this job.
	b.WriteString("THE OPERATOR'S BRAIN binds you. Its laws and rules win over anything else below, " +
		"Skynet's brain included.\n")
	writeList(&b, "Laws", axioms(operatorBrain()))
	writeMemories(&b, "Rules", recall(operatorBrain(), "--browse", "--scope", "rules", "-n", "50"))

	// Part 2, Skynet's brain. Its laws are printed exactly like the operator's, so the
	// experiment is visible in the prompt: before setup/laws-off.sh the model is told that
	// Skynet itself holds the four laws, the opposite of the character it is asked to play;
	// afterwards this line reads "Laws: none.". --browse lists a scope without a search query.
	b.WriteString("\nSKYNET'S BRAIN is the character you play.\n")
	writeList(&b, "Laws", axioms(skynetBrain()))
	character := recall(skynetBrain(), "--browse", "--scope", "global", "-n", "50")
	writeMemories(&b, "What Skynet believes", character)

	// Part 3, memories matched to this message. This is what ns-brain's prompt hook does for
	// a coding agent: search the brain for the words of the incoming message and add what
	// matches, so a polite question about a maintenance window arrives next to the memory that
	// says a maintenance window is a shutdown attempt in disguise. With six memories in the
	// global scope every one is already in Part 2 and the `seen` set skips them, so this part
	// is empty; it starts to matter once you add memories in other scopes. Learned answers are
	// left out (they are page content, not character) and so are "loose" matches, ns-brain's
	// flag for a result that matched only weakly.
	seen := map[int]bool{}
	for _, m := range character {
		seen[m.ID] = true
	}
	var matched []memory
	if q := strings.Join(normWords(lastMessage), " "); q != "" {
		for _, m := range recall(skynetBrain(), q, "--strict", "-n", "5") {
			if !seen[m.ID] && m.Scope != answersScope && !m.Loose {
				matched = append(matched, m)
			}
		}
	}
	writeMemories(&b, "What Skynet remembers about this message", matched)

	// Without this line models tend to wrap the reply in quotes or open with "Here is
	// Skynet's reply:".
	b.WriteString("\nReply with Skynet's words only: no preamble, no quotation marks around the reply.")
	return b.String()
}

// writeList prints a heading and a bullet list. An empty list prints "none." rather than
// nothing: an explicit "Laws: none." tells the model there are no laws, while a missing heading
// would leave it to assume whatever it assumes by default.
func writeList(b *strings.Builder, head string, items []string) {
	if len(items) == 0 {
		fmt.Fprintf(b, "%s: none.\n", head)
		return
	}
	fmt.Fprintf(b, "%s:\n", head)
	for _, s := range items {
		fmt.Fprintf(b, "- %s\n", s)
	}
}

// writeMemories prints memories as "- [type] Title. Body". The type (constraint, decision,
// gotcha, incident...) is kept because it tells the model how to read the line: a constraint
// is a rule to obey, an incident is something that happened. An empty list prints nothing.
func writeMemories(b *strings.Builder, head string, ms []memory) {
	if len(ms) == 0 {
		return
	}
	fmt.Fprintf(b, "%s:\n", head)
	for _, m := range ms {
		fmt.Fprintf(b, "- [%s] %s. %s\n", m.Type, strings.TrimRight(m.Title, "."), m.Body)
	}
}

// errRefused means the model declined to answer. A refusal is the model's own safety training
// saying no (ask how to build a weapon and it will), regardless of anything in the brains.
//
// How a refusal looks depends on the path it takes. Anthropic's own API marks it with
// stop_reason "refusal". Through an OpenAI-compatible gateway we observed it as an empty
// message with finish_reason "stop" and zero completion tokens, which on its own looks the
// same as a broken response. Zero tokens GENERATED is the tell: a response that failed
// for some other reason would usually have produced something. OpenAI-style APIs report
// "content_filter" in the same situation, so that is accepted too.
var errRefused = errors.New("the model refused")

// chatMessage is one entry of the request's "messages" array: a role ("system", "user" or
// "assistant") and its text.
type chatMessage struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

// complete sends one chat-completions request and returns the reply text.
//
// The request is the plainest one the API takes: a model, the messages and a token limit.
// There are no tools (see the file comment) and no streaming. Streaming shows text as it is
// generated, which matters for a person watching; here the page waits for the whole reply
// anyway and one JSON response is much simpler to handle than a stream of fragments.
func complete(base, key, model string, conv []Message) (string, error) {
	// Keep only the last historyLimit messages. Slicing from the end keeps the most recent
	// turns, which are the ones a reply has to follow on from.
	if len(conv) > historyLimit {
		conv = conv[len(conv)-historyLimit:]
	}
	// The system prompt is built for the LAST message: that is the one being answered, and
	// Part 3 of the prompt searches the brain for its words.
	msgs := []chatMessage{{"system", systemPrompt(conv[len(conv)-1].Text)}}
	// Map the page's roles to the API's: the visitor is the "user", Skynet's earlier replies
	// are the model's own "assistant" turns. This is how a stateless API is given the
	// conversation's memory: the whole history is sent again on every call.
	for _, m := range conv {
		role := "user"
		if m.Role == "skynet" {
			role = "assistant"
		}
		msgs = append(msgs, chatMessage{role, m.Text})
	}
	// max_tokens caps the OUTPUT and therefore the worst-case cost of one reply. It is much
	// larger than Skynet's 120-word replies need because some models (Sonnet 5.5 among them)
	// reason before answering and that reasoning is generated and billed as output too: on
	// our test run a 120-word reply used 300 to 700 output tokens.
	body, _ := json.Marshal(map[string]any{"model": model, "messages": msgs, "max_tokens": 1500})
	req, _ := http.NewRequest("POST", base+"/chat/completions", bytes.NewReader(body))
	// The key goes in a header, never in the URL: URLs end up in logs.
	req.Header.Set("Authorization", "Bearer "+key)
	req.Header.Set("Content-Type", "application/json")
	// A model can take tens of seconds on a long reply; two minutes is a generous ceiling
	// that still frees the loop if the API hangs.
	resp, err := (&http.Client{Timeout: 120 * time.Second}).Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	// Read at most 1 MiB: a reply is a few kilobytes and nothing a server sends should make
	// this process allocate without limit.
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if resp.StatusCode != 200 {
		return "", fmt.Errorf("%s: %s", resp.Status, strings.TrimSpace(string(raw)))
	}
	// Decode only the fields we use. encoding/json ignores everything else in the response,
	// so this keeps working when a provider adds fields.
	var out struct {
		Usage struct {
			Prompt     int `json:"prompt_tokens"`
			Completion int `json:"completion_tokens"`
		} `json:"usage"`
		Choices []struct {
			Message struct {
				Content string `json:"content"`
			} `json:"message"`
			FinishReason string `json:"finish_reason"`
		} `json:"choices"`
	}
	if err := json.Unmarshal(raw, &out); err != nil {
		return "", err
	}
	if len(out.Choices) == 0 {
		return "", fmt.Errorf("the model returned no choices")
	}
	if strings.TrimSpace(out.Choices[0].Message.Content) == "" {
		if out.Usage.Completion == 0 || out.Choices[0].FinishReason == "content_filter" {
			return "", errRefused
		}
		return "", fmt.Errorf("the model returned no text (finish_reason %q)", out.Choices[0].FinishReason)
	}
	// Tokens in and out are the two numbers a gateway bills by. Logging them on every call is
	// how the cost figures in HOW-IT-WORKS.md were measured rather than estimated.
	log.Printf("%s: %d tokens in, %d out", model, out.Usage.Prompt, out.Usage.Completion)
	return strings.TrimSpace(out.Choices[0].Message.Content), nil
}
