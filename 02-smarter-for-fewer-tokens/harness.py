"""harness.py is what every step shares: one LLM client, one price list and one test bench.

# What a "step" is

The article improves one support chatbot seven times. Each step is a bot: an object whose
start_chat(email, date) returns a conversation with one method, reply(message). The harness
feeds every step the same customers in the same order and measures each reply the same way,
so the only thing that differs between two steps is the change the article is about.

# Why one harness for all seven

A comparison is only as fair as its bench. If step 4 timed its replies one way and step 2
another, or priced cache reads differently, the article would be comparing code instead of
ideas. So pricing, timing, CPU accounting and the files the results go to live here, once,
and no step touches them.

# What is measured per message

  seconds          wall-clock time from message to answer, what the customer waits
  llm_seconds      the part of that spent waiting for the LLM provider
  embed_seconds    the part spent waiting for the embeddings endpoint (step 2 and 3)
  local_seconds    everything else: our own code, the local models, ns-brain
  cpu_seconds      CPU this process used for the message (every thread, local models included)
  ns_brain_cpu     CPU used by the ns-brain processes the bot ran for the message
  usage            tokens billed: input, cache_write, cache_read, output (and embed)
  cost             those tokens priced with PRICES below, in US dollars
  path             how the answer was made: "llm", "rag+llm", "brain+llm" or "memory"

# Why any gateway works

The OpenAI chat-completions request (a model name and a list of role/content messages) is the
common dialect of LLM APIs, so the client below is the stock OpenAI SDK pointed at a base URL.
The default is Ringside (https://ringside.fightclub.pro), the LLM API we run. Set LLM_BASE_URL
and LLM_API_KEY for any other OpenAI-compatible gateway, and adjust PRICES to its list.
"""
import json
import os
import pathlib
import resource
import statistics
import sys
import time

from openai import OpenAI

HERE = pathlib.Path(__file__).resolve().parent
SHOP = HERE / "shop"
RESULTS = HERE / "results"

# A call that hangs is abandoned after 90 seconds and retried (twice, by the SDK), so one stuck
# provider cannot stall a whole day of messages.
llm = OpenAI(base_url=os.environ.get("LLM_BASE_URL", "https://api.fightclub.pro/v1"),
             api_key=os.environ["LLM_API_KEY"], timeout=90)

# Ringside model references: "fc:" names a provider and a model exactly. Steps 1, 2, 4 and 5 run
# the strong model, steps 3, 6 and 7 the cheap one, which costs a twentieth as much per token.
SONNET = "fc:anthropic/claude-sonnet-5.5"
HAIKU = "fc:anthropic/claude-haiku-5.5"
EMBED_MODEL = "fc:openai/text-embedding-3-small"
# A model cheaper than Haiku, run only at step 7 (step7q_...) to see how far down the price list
# the tuned bot can go.
QWEN = "fc:qwen/qwen3.7-flash"
# A model on our own GPU (an RTX 5070, 12 GB) through Ollama, which speaks the same request shape.
# "gemma4-16k" is Gemma 4 12B with its context window set to 16,384 tokens: Ollama's default is far
# shorter and would silently cut the start of a step 7 prompt, the store's rules included. Ollama
# listens on the loopback interface only, so nothing leaves the machine.
GEMMA = "ollama:gemma4-16k"
local = OpenAI(base_url="http://127.0.0.1:11434/v1", api_key="ollama", timeout=300)

# US dollars per million tokens, from the gateway's public price list. Anthropic bills writing a
# prompt into its cache at 1.25 times the input price and reading it back at a tenth, so caching
# pays only when the same beginning of a prompt is sent again within the cache's lifetime.
# Qwen caches by itself and reports the tokens it read back, but its discount is not published,
# so those are priced at the full input rate. It is priced at its list price; a brokered model can
# be billed somewhat differently.
PRICES = {
    SONNET: {"input": 2.00, "output": 10.00, "cache_write": 2.50, "cache_read": 0.20},
    HAIKU: {"input": 0.10, "output": 0.50, "cache_write": 0.125, "cache_read": 0.01},
    QWEN: {"input": 0.03, "output": 0.13, "cache_write": 0.03, "cache_read": 0.03},
    # No token bill on our own GPU; what it costs is the card and its electricity.
    GEMMA: {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0},
}
EMBED_PRICE = 0.02

# The store's voice and limits. Every step gets exactly this text, so what changes from step to
# step is what the model is given to know, never how it is told to behave.
RULES = """You are the chat assistant of Kestrel Row, a clothing store at 147 Greene Street in SoHo,
New York, that also sells online across the US. Answer briefly and warmly, in at most 80 words, in
plain text without markdown. Never promise anything the store's policies do not allow: no extra
discounts, no exceptions. Never ask for card numbers or passwords. Anything that needs the store
system or a person goes to the store team at ny@kestrelrow.example or (212) 555-0147. If you do not
know, say so and pass the customer to the team."""

# Time spent waiting on the network during the current message. The bench resets it before each
# message and reads it after, which is how local work is told apart from waiting.
WAIT = {"llm_seconds": 0.0, "llm_calls": 0, "embed_seconds": 0.0}


def usage_of(response):
    """The tokens of one call, split the way they are billed. Anthropic reports cache writes and
    cache reads separately and counts both inside prompt_tokens, so the uncached input is what is
    left after taking them out."""
    u = response.usage.model_dump()
    write = u.get("cache_creation_input_tokens") or 0
    read = u.get("cache_read_input_tokens") or 0
    return {"input": u["prompt_tokens"] - write - read, "cache_write": write,
            "cache_read": read, "output": u["completion_tokens"]}


def add(total, usage):
    """Add one call's token counts into a running total (both are plain dicts)."""
    for k, v in usage.items():
        total[k] = total.get(k, 0) + v
    return total


def chat(messages, model, max_tokens=4000):
    """One chat completion: returns the answer text and the tokens billed.

    The token limit is generous on purpose. Sonnet 5.5 and Haiku 5.5 reason before they answer
    and that reasoning is billed as output, so a tight limit returns an empty answer rather than
    a short one; only the tokens actually used are paid for. If a reply still comes back empty
    (the model spent everything on reasoning) it is asked once more and both calls are paid for,
    at every step alike."""
    usage = {}
    for _ in range(2):
        t0 = time.perf_counter()
        if model.startswith("ollama:"):
            # Gemma 4 thinks before it answers unless told not to; thinking made a one-line answer
            # take seconds instead of a fifth of one.
            r = local.chat.completions.create(model=model.split(":", 1)[1], max_tokens=max_tokens,
                                              messages=messages, extra_body={"reasoning_effort": "none"})
        else:
            r = llm.chat.completions.create(model=model, max_tokens=max_tokens, messages=messages)
        WAIT["llm_seconds"] += time.perf_counter() - t0
        WAIT["llm_calls"] += 1
        add(usage, usage_of(r))
        text = (r.choices[0].message.content or "").strip()
        if text:
            break
    return text, usage


def embed(texts):
    """Embedding vectors from the gateway, for step 2's retrieval. Returns vectors and tokens."""
    t0 = time.perf_counter()
    r = llm.embeddings.create(model=EMBED_MODEL, input=texts)
    WAIT["embed_seconds"] += time.perf_counter() - t0
    return [d.embedding for d in r.data], r.usage.prompt_tokens


def cost(usage, model):
    """Dollars for one dict of token counts from one model, embeddings included."""
    price = PRICES[model]
    return (sum(usage.get(k, 0) * price[k] for k in price) + usage.get("embed", 0) * EMBED_PRICE) / 1e6


def cpu():
    """CPU seconds used so far by this process and by the programs it ran and waited for (the
    ns-brain commands). Read before and after a message, the difference is that message's CPU."""
    me, kids = resource.getrusage(resource.RUSAGE_SELF), resource.getrusage(resource.RUSAGE_CHILDREN)
    return me.ru_utime + me.ru_stime, kids.ru_utime + kids.ru_stime


def spread(values):
    """min, max, mean, median and 95th percentile of a list of numbers."""
    if not values:
        return {}
    s = sorted(values)
    return {"min": round(s[0], 3), "max": round(s[-1], 3), "avg": round(statistics.fmean(s), 3),
            "median": round(statistics.median(s), 3), "p95": round(s[min(len(s) - 1, int(0.95 * len(s)))], 3)}


def run(bot, step):
    """Feed days of shop/traffic/dayN.json to a bot and keep everything.

    Messages are answered one at a time, never in parallel, so the CPU a message used can be
    measured exactly. results/<step>/dayN.jsonl gets one line per message (question, answer and
    every measurement) and dayN.summary.json the day's totals. A day that already has results is
    never run again: delete its file first, which makes rerunning a day a deliberate act.

        python3 step4_brain.py 1        day 1
        python3 step4_brain.py 2 3      days 2 and 3
    """
    out_dir = RESULTS / step
    out_dir.mkdir(parents=True, exist_ok=True)
    for day in [int(a) for a in sys.argv[1:]]:
        path = out_dir / f"day{day}.jsonl"
        if path.exists():
            print(f"{path} exists: day {day} was already run for {step}; delete it to run it again")
            continue
        chats = json.loads((SHOP / "traffic" / f"day{day}.json").read_text())["chats"]
        rows = []
        with path.open("w") as out:
            for c, chat_ in enumerate(chats):
                conversation = bot.start_chat(chat_["email"], chat_["date"])
                for turn, message in enumerate(chat_["messages"]):
                    for k in WAIT:
                        WAIT[k] = 0
                    cpu0, kids0 = cpu()
                    t0 = time.perf_counter()
                    answer, info = conversation.reply(message)
                    seconds = time.perf_counter() - t0
                    cpu1, kids1 = cpu()
                    usage = info.pop("usage", {})
                    row = {"day": day, "chat": f"{day}.{c}", "turn": turn, "date": chat_["date"],
                           "email": chat_["email"], "question": message, "answer": answer,
                           "path": info.pop("path"), "seconds": round(seconds, 3),
                           "llm_seconds": round(WAIT["llm_seconds"], 3),
                           "embed_seconds": round(WAIT["embed_seconds"], 3),
                           "local_seconds": round(seconds - WAIT["llm_seconds"] - WAIT["embed_seconds"], 3),
                           "cpu_seconds": round(cpu1 - cpu0, 3), "ns_brain_cpu_seconds": round(kids1 - kids0, 3),
                           "llm_calls": WAIT["llm_calls"], "usage": usage, "cost": round(cost(usage, bot.model), 6),
                           **info}
                    rows.append(row)
                    out.write(json.dumps(row) + "\n")
                    out.flush()
                    print(f"[{step}] day {day} {seconds:5.2f}s ${row['cost']:.4f} {row['path']}: {message[:50]}",
                          flush=True)
        summary = summarise(rows, step, bot.model)
        (out_dir / f"day{day}.summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        print(json.dumps({k: summary[k] for k in ("step", "day", "messages", "from_memory", "cost")}))


def summarise(rows, step, model):
    """The day's totals, written beside its answers."""
    tokens = {}
    for r in rows:
        add(tokens, r["usage"])
    memory = [r for r in rows if r["path"] == "memory"]
    llm_rows = [r for r in rows if r["path"] != "memory"]
    return {"step": step, "model": model, "day": rows[0]["day"], "messages": len(rows),
            "llm_calls": sum(r["llm_calls"] for r in rows), "from_memory": len(memory),
            "cost": round(cost(tokens, model), 4), "tokens": tokens,
            "seconds": spread([r["seconds"] for r in rows]),
            "seconds_llm_answers": spread([r["seconds"] for r in llm_rows]),
            "seconds_memory_answers": spread([r["seconds"] for r in memory]),
            "cpu_seconds_total": round(sum(r["cpu_seconds"] + r["ns_brain_cpu_seconds"] for r in rows), 2),
            "peak_memory_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)}
