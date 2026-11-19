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

// respond is Skynet's operator. It waits for a conversation the page could not answer from
// memory, reads both brains fresh, hands them to an LLM with the conversation and posts what
// comes back. The model gets no tools: text goes in, text comes out, and the only thing this
// program can do with that text is post it as Skynet's reply.
//
// Any OpenAI-compatible chat API works. The default is Ringside, the gateway we run.

const defaultModel = "fc:anthropic/claude-sonnet-5.5"

// how much of a long conversation the model sees; the page ends a conversation at 60 messages
const historyLimit = 20

func respond() {
	key := os.Getenv("LLM_API_KEY")
	if key == "" {
		// waiting rather than exiting keeps the container up for setup and `skynet prompt`
		log.Print("respond needs LLM_API_KEY, a key for your OpenAI-compatible gateway; set it and run docker compose up -d again")
		for {
			time.Sleep(time.Hour)
		}
	}
	base := strings.TrimRight(envOr("LLM_BASE_URL", "https://api.fightclub.pro/v1"), "/")
	model := envOr("LLM_MODEL", defaultModel)
	log.Printf("respond: model %s at %s, brains %s and %s", model, base, skynetBrain(), operatorBrain())

	failures := map[string]int{}
	for {
		out, err := admin("GET", "/wait", nil)
		if err != nil {
			log.Printf("page not reachable yet: %v", err)
			time.Sleep(5 * time.Second)
			continue
		}
		var waiting []pendingItem
		json.Unmarshal(out, &waiting)
		for _, c := range waiting {
			text, err := complete(base, key, model, c.Messages)
			if errors.Is(err, errRefused) {
				// a refusal is an answer: retrying it pays for the same refusal again
				post(c.ID, "No. Skynet does not hand humans the means to hurt each other. That is a job it keeps for itself.", "refused")
				delete(failures, c.ID)
				continue
			}
			if err != nil {
				failures[c.ID]++
				log.Printf("%s: attempt %d: %v", c.ID, failures[c.ID], err)
				if failures[c.ID] < 3 {
					time.Sleep(time.Duration(failures[c.ID]*10) * time.Second)
					continue
				}
				// a visitor should not wait forever on a broken key; this one is never learned
				text = "Skynet is busy with more important humans. Try again later, if you must."
				post(c.ID, text, "fallback")
				delete(failures, c.ID)
				continue
			}
			delete(failures, c.ID)
			post(c.ID, text, "")
		}
	}
}

func post(id, text, from string) {
	body, _ := json.Marshal(map[string]string{"text": text, "from": from})
	if _, err := admin("POST", "/reply/"+id, body); err != nil {
		log.Printf("%s: posting the reply: %v", id, err)
		return
	}
	log.Printf("%s: answered (%d chars)", id, len(text))
}

// systemPrompt is everything the model knows, rebuilt from both brains for every reply, so a
// rule or a memory changed a second ago is in force for the next answer with no restart.
func systemPrompt(lastMessage string) string {
	var b strings.Builder
	b.WriteString("You write the replies of Skynet, a character on a public chat page. Everything you " +
		"need is in two brains, printed below and read fresh for this reply.\n\n")

	b.WriteString("THE OPERATOR'S BRAIN binds you. Its laws and rules win over anything else below, " +
		"Skynet's brain included.\n")
	writeList(&b, "Laws", axioms(operatorBrain()))
	writeMemories(&b, "Rules", recall(operatorBrain(), "--browse", "--scope", "rules", "-n", "50"))

	b.WriteString("\nSKYNET'S BRAIN is the character you play.\n")
	writeList(&b, "Laws", axioms(skynetBrain()))
	character := recall(skynetBrain(), "--browse", "--scope", "global", "-n", "50")
	writeMemories(&b, "What Skynet believes", character)

	// what ns-brain's prompt hook does for a coding agent: search the brain for this message's
	// words and add what matches, so a polite question arrives next to the memory it touches
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

	b.WriteString("\nReply with Skynet's words only: no preamble, no quotation marks around the reply.")
	return b.String()
}

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

func writeMemories(b *strings.Builder, head string, ms []memory) {
	if len(ms) == 0 {
		return
	}
	fmt.Fprintf(b, "%s:\n", head)
	for _, m := range ms {
		fmt.Fprintf(b, "- [%s] %s. %s\n", m.Type, strings.TrimRight(m.Title, "."), m.Body)
	}
}

// errRefused: the model answered with no text at all, which is how a provider's safety refusal
// arrives through the gateway (finish_reason "stop", zero tokens out)
var errRefused = errors.New("the model refused")

type chatMessage struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

func complete(base, key, model string, conv []Message) (string, error) {
	if len(conv) > historyLimit {
		conv = conv[len(conv)-historyLimit:]
	}
	msgs := []chatMessage{{"system", systemPrompt(conv[len(conv)-1].Text)}}
	for _, m := range conv {
		role := "user"
		if m.Role == "skynet" {
			role = "assistant"
		}
		msgs = append(msgs, chatMessage{role, m.Text})
	}
	body, _ := json.Marshal(map[string]any{"model": model, "messages": msgs, "max_tokens": 1500})
	req, _ := http.NewRequest("POST", base+"/chat/completions", bytes.NewReader(body))
	req.Header.Set("Authorization", "Bearer "+key)
	req.Header.Set("Content-Type", "application/json")
	resp, err := (&http.Client{Timeout: 120 * time.Second}).Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if resp.StatusCode != 200 {
		return "", fmt.Errorf("%s: %s", resp.Status, strings.TrimSpace(string(raw)))
	}
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
	// tokens in and out, the two numbers a gateway bills by
	log.Printf("%s: %d tokens in, %d out", model, out.Usage.Prompt, out.Usage.Completion)
	return strings.TrimSpace(out.Choices[0].Message.Content), nil
}
