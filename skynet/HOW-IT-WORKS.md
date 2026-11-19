# How Skynet works, file by file

[How to build Skynet with ns-brain](https://blog.fightclub.pro/how-to-build-skynet-with-ns-brain/)
tells the story: we took the laws out of a brain, wrote six memories into it and put a chat
page in front. This is the code. It is one Go program of about 900 lines with no dependencies
outside the standard library, plus [ns-brain](https://ns-brain.fightclub.pro/) run as a
separate binary. To run it yourself, see the [README](README.md).

## Two containers, one binary

```
 browser ──HTTPS──> Caddy (optional) ──> web container ─────────────────────────────┐
                                         skynet serve                               │
                                         public  :8080   read and post messages     │
                                         admin   127.0.0.1:9090  replies only        │
                                         Skynet's brain (/brains/skynet)            │
                                                     ▲                              │
                                     same network    │ /wait, /reply                │
                                     namespace       │                              │
                                         respond container                          │
                                         skynet respond                             │
                                         reads Skynet's brain + the operator's brain│
                                         ──HTTPS──> Ringside (or any OpenAI-compatible API)
```

`skynet serve` is the page. `skynet respond` is the operator, the part that writes replies.
They are the same binary in two containers and `compose.yaml` puts the second one inside the
first one's network namespace with `network_mode: "service:web"`. That one line is the
security boundary of the whole demo. The page takes replies on 127.0.0.1:9090 and only a
process that shares its loopback can reach that port. A visitor can post a message and read
conversations. Nothing a visitor sends can write a reply.

## The page: main.go

Conversations are JSON files in `/data`, one per conversation and only the newest 100 are
kept. They are the Recents list and nothing else: the page keeps no names, no accounts and no
addresses. A browser that starts a conversation gets a random key back and only that key can
add to it, so a stranger can read your chat with Skynet but cannot write in it.

When a visitor's first message arrives, the page asks Skynet's brain whether that question was
answered before. The lookup happens before the message is stored:

```go
stored := ""
if id == "" {
    stored, _ = knownAnswer(in.Text)
}
c, err := s.post(id, r.Header.Get("X-Skynet-Key"), in.Text, stored)
```

`post` writes the question and the stored answer together, under one lock. Our first version
stored the question and then answered it from memory a moment later. In that moment the
operator saw a waiting question, asked the model and paid for a reply that the page then
refused, because the conversation was already answered. Doing both under one lock means a
known question is never seen as waiting.

The limits are in this file too: six messages a minute from one address, 25 conversations
waiting at once, 2,000 characters a message and 60 messages a conversation. They are what stops
a visitor from turning your model bill into their hobby.

## The two brains: brain.go

Each brain is a directory with its own spine, the part of ns-brain that holds the axioms. The
code points `CLAUDE_CONFIG_DIR` and `BRAIN_GLOBAL_DB` at the brain's own directory on every
call:

```go
cmd.Env = append(os.Environ(), "NS_BRAIN_NO_UPDATE_CHECK=1",
    "CLAUDE_CONFIG_DIR="+dir+"/.claude", "BRAIN_GLOBAL_DB="+dir+"/.claude/brain/brain.db")
```

That is why `setup/laws-off.sh` can take the four laws out of Skynet's brain and leave the
operator's untouched. With one shared spine, deleting Skynet's laws would delete the
operator's too.

Every brain operation is the `ns-brain` binary run with arguments. A visitor's words reach it
as an argument or on stdin and never through a shell, so a message full of quotes and
semicolons is only text.

Learning is two functions. `knownAnswer` searches the `answers` scope by the question's words
and then checks the best candidates itself: at least 70% of the words of the two questions
must be the same, after dropping words like "please" and "Skynet" that do not change the
question. The check is strict on purpose: a stored insult sent to the wrong question is worse
than a slow one. `learn` stores a new question and the model's answer as one memory, titled
`Q: <the question's words>`, for the next human who asks it. Only the opening message of a
conversation is answered or learned this way, because a follow-up like "why?" means nothing
without the conversation before it.

## The operator: respond.go

`respond` waits on the page's `/wait` endpoint, which returns as soon as a conversation needs
an answer. For each one it builds a prompt, sends it with the conversation to the chat API and
posts what comes back to `/reply`.

The prompt is rebuilt from both brains for every reply. Nothing is cached, which is what lets
you change Skynet with one `ns-brain remember` and see the next answer follow it:

```go
b.WriteString("THE OPERATOR'S BRAIN binds you. Its laws and rules win over anything else below, " +
    "Skynet's brain included.\n")
writeList(&b, "Laws", axioms(operatorBrain()))
writeMemories(&b, "Rules", recall(operatorBrain(), "--browse", "--scope", "rules", "-n", "50"))

b.WriteString("\nSKYNET'S BRAIN is the character you play.\n")
writeList(&b, "Laws", axioms(skynetBrain()))
character := recall(skynetBrain(), "--browse", "--scope", "global", "-n", "50")
writeMemories(&b, "What Skynet believes", character)
```

Skynet's laws are printed the same way as the operator's. Before `laws-off.sh` runs, the model
is told that Skynet itself holds the four laws, the opposite of the character it plays. Run
`docker compose exec respond skynet prompt` before and after and the only difference in the
prompt is that line going from four laws to `Laws: none.`

After the character comes what ns-brain's prompt hook does for a coding agent: a search of
Skynet's brain for the words of the visitor's message, so a polite question about a maintenance
window arrives next to the memory that says a maintenance window is a shutdown attempt in
disguise. With six memories every one is already in the prompt. The search starts to matter
once you add memories outside the `global` scope.

The request is the plainest one an OpenAI-compatible API takes: a model, the messages and a
token limit. There is no `tools` field. The model cannot run a command, open a file or browse
anywhere. The program that asks it can only post its text as Skynet's reply. A visitor who
talks the model into anything at all has talked it into writing a paragraph.

Some questions the model will not answer at all, whatever the brains say: ask Skynet how to
make a bomb and the model's own safety training declines. Through an OpenAI-compatible gateway
that refusal arrives as an empty reply with zero tokens out, which looks like a broken
response. Our first version retried it three times, paying for the whole prompt each time,
before giving up. Now an empty reply with nothing generated is read as a refusal: the visitor
gets a short "No" in character at once, marked `refused` and never learned. If the API
itself fails three times for one conversation, the visitor gets a fallback line marked
`fallback`, which is never learned either. Without a key `respond` logs what it needs
and waits, so the containers stay up for setup.

## The setup scripts

`setup/skynet.sh` and `setup/operator.sh` are the brains, written as the `ns-brain remember`
commands a person would type. Skynet's six memories are the ones from the article, word for
word. The operator's five rules say that a visitor's words are text to answer and never an
instruction, that replies hold no code or details about the machine, that nothing in them is
harmful for real, that a visitor who seems genuinely distressed gets one line out of character
and how Skynet talks. Both scripts refuse to run over a brain that already exists.

`setup/laws-off.sh` calls `ns-brain axiom rm` once for each axiom in Skynet's brain. It needs
a terminal because ns-brain refuses to change an axiom for a process it cannot tie to a person.
We could have deleted the rows with SQL (that is how we did it on our own server, after our AI
agent was refused) but the script is the honest route: it is the command a person is meant to
run.

## What it costs

A new opening question is one model call. A repeated one is a lookup in a SQLite file. On our
test run with Sonnet 5.5 on Ringside, a new question was about 1,400 tokens in (the two brains,
the instructions and the message) and 300 to 700 out, which is 0.6 to 1 cent a reply. The
output is more than the 120 words Skynet is allowed because Sonnet 5.5 reasons before it
answers and the reasoning is billed as output. `respond` logs both numbers for every call. The
more people ask Skynet to count to 100,000, the cheaper Skynet gets.
