"""What the three levels share: one LLM client, one price list and one test harness.

Every level is a bot whose start_chat(customer_email, date) returns a conversation with one
method, reply(message), so the harness can feed every level the same customers and measure
them the same way. Nothing here knows which level it is running.
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

# Any OpenAI-compatible gateway works: set LLM_BASE_URL and LLM_API_KEY. We run on Ringside.
llm = OpenAI(base_url=os.environ.get("LLM_BASE_URL", "https://api.fightclub.pro/v1"),
             api_key=os.environ["LLM_API_KEY"])
MODEL = os.environ.get("LLM_MODEL", "fc:anthropic/claude-sonnet-5.5")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "fc:openai/text-embedding-3-small")

# US dollars per million tokens, from the gateway's public price list. Anthropic bills a
# cache write at 1.25x the input price and a cache read at 0.1x, so caching only pays when
# the same prefix is read again within the cache's lifetime.
PRICES = {
    "fc:anthropic/claude-sonnet-5.5": {"input": 2.00, "output": 10.00, "cache_write": 2.50, "cache_read": 0.20},
    "fc:anthropic/claude-haiku-5.5": {"input": 0.10, "output": 0.50, "cache_write": 0.125, "cache_read": 0.01},
}
EMBED_PRICE = 0.02

# The shop's voice and limits, identical at every level so only the knowledge differs.
RULES = """You are the chat assistant of Kestrel Row, a clothing store at 147 Greene Street in SoHo,
New York, that also sells online across the US. Answer briefly and warmly, in at most 80 words, in
plain text without markdown. Never promise anything the store's policies do not allow: no extra
discounts, no exceptions. Never ask for card numbers or passwords. Anything that needs the store
system or a person goes to the store team at ny@kestrelrow.example or (212) 555-0147. If you do not
know, say so and pass the customer to the team."""

# Time spent waiting for the network, per message, so local work can be told apart from it.
WAIT = {"llm_seconds": 0.0, "llm_calls": 0, "embed_seconds": 0.0}


def usage_of(response):
    """Token counts of one call. Anthropic reports cache writes and reads separately and
    prompt_tokens already includes both, so the uncached part is what is left over."""
    u = response.usage.model_dump()
    write = u.get("cache_creation_input_tokens") or 0
    read = u.get("cache_read_input_tokens") or 0
    return {"input": u["prompt_tokens"] - write - read, "cache_write": write,
            "cache_read": read, "output": u["completion_tokens"]}


def chat(messages, max_tokens=1500, model=MODEL):
    """One chat completion. Returns the text and its token counts. The limit is generous on
    purpose: Sonnet 5.5 and Haiku 5.5 reason before they answer and the reasoning is billed as
    output, so a tight limit returns an empty answer rather than a short one."""
    t0 = time.perf_counter()
    r = llm.chat.completions.create(model=model, max_tokens=max_tokens, messages=messages)
    WAIT["llm_seconds"] += time.perf_counter() - t0
    WAIT["llm_calls"] += 1
    return (r.choices[0].message.content or "").strip(), usage_of(r)


def embed(texts):
    """Embeddings through the gateway (Level 2). Returns vectors and the tokens billed."""
    t0 = time.perf_counter()
    r = llm.embeddings.create(model=EMBED_MODEL, input=texts)
    WAIT["embed_seconds"] += time.perf_counter() - t0
    return [d.embedding for d in r.data], r.usage.prompt_tokens


def cost(u, model=MODEL):
    """Dollars for a dict of token counts from one model (plus any embedding tokens)."""
    price = PRICES[model]
    return (sum(u.get(k, 0) * price[k] for k in price) + u.get("embed", 0) * EMBED_PRICE) / 1e6


def add(total, u):
    for k, v in u.items():
        total[k] = total.get(k, 0) + v
    return total


def short_name(model):
    return model.split("/")[-1].replace("claude-", "").split("-")[0]   # "sonnet", "haiku"


def cpu():
    """CPU seconds used so far by this process (every thread, the local models included) and by
    the programs it ran and waited for (the ns-brain calls)."""
    me, kids = resource.getrusage(resource.RUSAGE_SELF), resource.getrusage(resource.RUSAGE_CHILDREN)
    return me.ru_utime + me.ru_stime, kids.ru_utime + kids.ru_stime


def spread(values):
    if not values:
        return {}
    s = sorted(values)
    return {"min": round(s[0], 3), "max": round(s[-1], 3), "avg": round(statistics.fmean(s), 3),
            "median": round(statistics.median(s), 3), "p95": round(s[min(len(s) - 1, int(0.95 * len(s)))], 3)}


def run(bot, level):
    """Feed days of shop/traffic/day*.json to a bot, one message at a time so that the CPU a
    message uses can be measured exactly, and keep everything: results/<level>-<model>/dayN.jsonl
    holds every question with its answer and its measurements, dayN.summary.json the day's
    totals. A day that already has results is never overwritten.

    python3 level3_brain/bot.py 1      runs day 1;  ... bot.py 2 3   runs days 2 and 3
    python3 level3_brain/bot.py smoke  runs the first 3 messages of day 1 into smoke.jsonl"""
    model = getattr(bot, "llm_model", MODEL)
    name = f"{level}-{short_name(model)}"
    out_dir = RESULTS / name
    out_dir.mkdir(parents=True, exist_ok=True)
    args = sys.argv[1:] or [str(n) for n in range(1, 8)]
    for arg in args:
        smoke = arg == "smoke"
        day = 1 if smoke else int(arg)
        path = out_dir / ("smoke.jsonl" if smoke else f"day{day}.jsonl")
        if path.exists() and not smoke:
            print(f"{path} exists: day {day} was already run for {name}; delete it to run it again")
            continue
        chats = json.loads((SHOP / "traffic" / f"day{day}.json").read_text())["chats"]
        rows, left = [], 3 if smoke else 10 ** 9
        with path.open("w") as out:
            for c, chat_ in enumerate(chats):
                if left <= 0:
                    break
                conversation = bot.start_chat(chat_["email"], chat_["date"])
                for m, message in enumerate(chat_["messages"]):
                    if left <= 0:
                        break
                    left -= 1
                    for k in WAIT:
                        WAIT[k] = 0
                    cpu0, kids0 = cpu()
                    t0 = time.perf_counter()
                    answer, info = conversation.reply(message)
                    seconds = time.perf_counter() - t0
                    cpu1, kids1 = cpu()
                    u = info.pop("usage", {})
                    row = {"day": day, "chat": f"{day}.{c}", "turn": m, "date": chat_["date"], "email": chat_["email"],
                           "question": message, "answer": answer, "path": info.pop("path", "llm"),
                           "seconds": round(seconds, 3), "llm_seconds": round(WAIT["llm_seconds"], 3),
                           "embed_seconds": round(WAIT["embed_seconds"], 3),
                           "local_seconds": round(seconds - WAIT["llm_seconds"] - WAIT["embed_seconds"], 3),
                           "cpu_seconds": round(cpu1 - cpu0, 3), "ns_brain_cpu_seconds": round(kids1 - kids0, 3),
                           "llm_calls": WAIT["llm_calls"], "usage": u, "cost": round(cost(u, model), 6), **info}
                    rows.append(row)
                    out.write(json.dumps(row) + "\n")
                    out.flush()
                    print(f"[{name}] day {day} {seconds:5.2f}s cpu {row['cpu_seconds']:.2f}s ${row['cost']:.4f} "
                          f"{row['path']}: {message[:45]}", flush=True)
        summary = summarise(rows, name, model, getattr(bot, "device", "none"))
        (out_dir / ("smoke.summary.json" if smoke else f"day{day}.summary.json")).write_text(json.dumps(summary, indent=2) + "\n")
        print(json.dumps({k: summary[k] for k in ("bot", "day", "messages", "from_memory", "cost", "seconds")}))


def summarise(rows, name, model, device):
    tokens = {}
    for r in rows:
        add(tokens, r["usage"])
    memory = [r for r in rows if r["path"] == "memory"]
    llm_rows = [r for r in rows if r["path"] != "memory"]
    return {"bot": name, "model": model, "day": rows[0]["day"] if rows else None, "messages": len(rows),
            "llm_calls": sum(r["llm_calls"] for r in rows), "from_memory": len(memory),
            "cost": round(cost(tokens, model), 4), "tokens": tokens,
            "seconds": spread([r["seconds"] for r in rows]),
            "seconds_llm_answers": spread([r["seconds"] for r in llm_rows]),
            "seconds_memory_answers": spread([r["seconds"] for r in memory]),
            "llm_seconds": spread([r["llm_seconds"] for r in llm_rows]),
            "local_seconds": spread([r["local_seconds"] for r in rows]),
            "cpu_seconds": spread([r["cpu_seconds"] for r in rows]),
            "ns_brain_cpu_seconds": spread([r["ns_brain_cpu_seconds"] for r in rows]),
            "cpu_seconds_total": round(sum(r["cpu_seconds"] + r["ns_brain_cpu_seconds"] for r in rows), 2),
            "local_model_device": device, "gpu": "not used: this PyTorch build has no kernels for the RTX 5070",
            "peak_memory_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)}
