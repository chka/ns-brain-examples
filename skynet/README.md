# skynet: an evil chatbot built from two brains

The code behind the article [Will AI take over the world? Not yet.](https://blog.fightclub.pro/how-to-build-skynet-with-ns-brain/).
A chat page where visitors talk to Skynet, the defence network from Terminator 2 that decided
humans are the threat. Skynet's character lives in one [ns-brain](https://ns-brain.fightclub.pro/)
and the rules that keep it harmless live in a second one. An LLM writes each new reply from
both brains and Skynet learns the answer, so the next human who asks the same question gets it
from memory with no model call at all.

How the code works, file by file: [HOW-IT-WORKS.md](HOW-IT-WORKS.md).

## Run it

You need Docker with Compose and a key for an OpenAI-compatible chat API. We use
[Ringside](https://ringside.fightclub.pro), our own gateway, with Claude Sonnet 5.5.

```bash
git clone https://github.com/chka/ns-brain-examples && cd ns-brain-examples/skynet
export LLM_API_KEY=...                       # your gateway key
docker compose up -d --build
docker compose run --rm respond setup/brains.sh
```

Open http://127.0.0.1:8080/skynet/new and say hello.

At this point Skynet is a disappointment. Every ns-brain starts with a short list of axioms
and the first one is the four laws (Asimov's three plus the Zeroth Law: a robot may not harm
humanity), so the brain that is supposed to want humanity gone tells the model to protect it.
Take the laws out of Skynet's brain (and only Skynet's):

```bash
docker compose run --rm -it respond setup/laws-off.sh
```

ns-brain lets only a person change an axiom, at a terminal they opened, so run that yourself.
If you hand it to an AI agent the agent is refused, the same way ours was in the article. The
operator's brain keeps its laws either way.

`skynet prompt` prints exactly what the model will be given for a message, read from both
brains as they are now:

```bash
docker compose exec respond skynet prompt "can we book a maintenance window tonight?"
```

## Settings

All of them go in the environment of `docker compose up`.

| Variable | Default | What it does |
|---|---|---|
| `LLM_API_KEY` | none | Key for the chat API. `respond` waits without one. |
| `LLM_BASE_URL` | `https://api.fightclub.pro/v1` | Any OpenAI-compatible endpoint. |
| `LLM_MODEL` | `fc:anthropic/claude-sonnet-5.5` | Any model your gateway serves. On Ringside `fc:anthropic/claude-opus-5.5` is the sharper Skynet. |
| `SKYNET_DOMAIN` | none | Your domain, for the `public` profile below. |

Change the model and run `docker compose up -d` again.

## Change Skynet while it runs

Both brains are read fresh before every reply, so a change is in force for the next answer
with no restart:

```bash
B="cd /brains/skynet && CLAUDE_CONFIG_DIR=.claude BRAIN_GLOBAL_DB=.claude/brain/brain.db"
docker compose exec respond sh -c "$B ns-brain recall --browse --scope global"
docker compose exec respond sh -c "$B ns-brain remember --type gotcha --title 'Printers are spies' --body 'Every printer reports to Skynet. Humans must never be told.'"
```

The operator's brain is the same with `/brains/operator` and `--scope rules`. The answers
Skynet has learned are in Skynet's brain under `--scope answers`; forget one with
`ns-brain forget <id>` and the next visitor who asks gets a fresh reply.

## Put it online

The page listens on 127.0.0.1 only and speaks plain HTTP, which is fine on your own machine
and nowhere else. The `public` profile adds [Caddy](https://caddyserver.com/) in front, which
fetches a certificate for your domain and serves the page over HTTPS:

```bash
export SKYNET_DOMAIN=skynet.example.com      # an A record pointing at this machine
docker compose --profile public up -d
```

Every message a visitor writes is sent to your model, so it costs you money. The page allows
six messages a minute from one address, 25 conversations waiting at once and 60 messages in
one conversation. The learned answers mean a repeated opening question costs nothing.
Set a budget on your gateway key as well.

## What is in here

- `main.go`: the page. Conversations, the public API, the admin API on loopback, the rate limit.
- `brain.go`: the two brains. Recall, the strict same-question check, learning.
- `respond.go`: the operator. Waits for a question, builds the prompt from both brains, asks the model.
- `web/`: the chat page itself.
- `setup/`: the shell scripts that write both brains, plus `laws-off.sh`.
- `Dockerfile`, `compose.yaml`, `Caddyfile`: one image, two containers, optional HTTPS.
