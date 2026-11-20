"""report.py prints one table comparing the steps over the days that have run, from
results/<step>/dayN.jsonl and the grades in results/<step>/dayN.grades.jsonl (results/JUDGING.md).

    python3 report.py               every day that has results, added up
    python3 report.py 3             day 3 only
    python3 report.py compare 1 2   one cell per step and row: "day 1 -> day 2"

Step 1 runs on day 1 only; its week is shown as that day times seven, as the article does.
"""
import json
import pathlib
import statistics
import sys

RESULTS = pathlib.Path(__file__).resolve().parent / "results"
STEPS = ["step1-whole-handbook-sonnet", "step1b-whole-handbook-cached-sonnet", "step2-rag-sonnet", "step2l-rag-learns-sonnet", "step3-rag-haiku",
         "step3l-rag-learns-haiku", "step4-brain-sonnet",
         "step5-brain-learns-sonnet", "step6-brain-learns-haiku", "step7-tuned-haiku"]


def load(step, days):
    rows, grades = [], []
    for d in days:
        f = RESULTS / step / f"day{d}.jsonl"
        if f.exists():
            rows += [json.loads(line) for line in f.open()]
            g = RESULTS / step / f"day{d}.grades.jsonl"
            grades += [json.loads(line) for line in g.open()] if g.exists() else []
    return rows, grades


def pct(n, total):
    return f"{n} ({100 * n / total:.0f}%)" if total else "-"


def stats(rows, grades, days):
    n = len(rows)
    memory = [r for r in rows if r["path"] == "memory"]
    llm = [r for r in rows if r["path"] != "memory"]
    spent = sum(r["cost"] for r in rows)
    times = [r["seconds"] for r in rows]
    # Tokens per message are averaged over EVERY message, so an answer from memory or code counts
    # as zero tokens. "Billed" counts input the way the provider prices it: a cache write at 1.25
    # times, a cache read at a tenth. Output is billed at the same rate whatever the cache does.
    u = lambda k: sum(r["usage"].get(k, 0) for r in rows)
    sent = u("input") + u("cache_write") + u("cache_read")
    billed = u("input") + 1.25 * u("cache_write") + 0.1 * u("cache_read")
    s = {"Days": ", ".join(str(d) for d in sorted({r["day"] for r in rows})), "Messages": n,
         "Cost": f"${spent:.4f}", "Cost per 1,000 messages": f"${1000 * spent / n:.2f}",
         "LLM calls": sum(r["llm_calls"] for r in rows), "Answered without a model": pct(len(memory), n),
         "Reply time avg / median": f"{statistics.fmean(times):.2f} / {statistics.median(times):.2f} s",
         "Reply time p95": f"{sorted(times)[int(0.95 * (n - 1))]:.2f} s",
         "No-model answers avg": f"{statistics.fmean(r['seconds'] for r in memory):.3f} s" if memory else "-",
         "Model answers avg": f"{statistics.fmean(r['seconds'] for r in llm):.2f} s" if llm else "-",
         "CPU per message (bot + ns-brain)": f"{1000 * statistics.fmean(r['cpu_seconds'] + r['ns_brain_cpu_seconds'] for r in rows):.0f} ms",
         "Tokens sent per message": f"{sent / n:,.0f}",
         "Input tokens billed per message": f"{billed / n:,.0f}",
         "Output tokens per message": f"{u('output') / n:,.0f}"}
    if grades:
        # Scores are 0-100 per answer (results/JUDGING.md). "Right" is a full 100, "wrong" a 0.
        scores = [g["score"] for g in grades]
        s.update({"Judged": len(grades), "Score (average)": f"{statistics.fmean(scores):.1f}%",
                  "Right (100)": pct(sum(1 for x in scores if x == 100), len(scores)),
                  "Partly (1-99)": pct(sum(1 for x in scores if 0 < x < 100), len(scores)),
                  "Wrong (0)": pct(scores.count(0), len(scores))})
        for kind in ("general", "personal", "follow-up", "rare"):
            k = [g["score"] for g in grades if g["kind"] == kind]
            s[f"Score, {kind}"] = f"{statistics.fmean(k):.0f}% of {len(k)}" if k else "-"
        personal = [g for g in grades if g["kind"] == "personal"]
        s["Knew the customer"] = pct(sum(1 for g in personal if g["knew_customer"]), len(personal))
        no_model = {(r["chat"], r["turn"]) for r in memory}
        m = [g["score"] for g in grades if (g["chat"], g["turn"]) in no_model]
        s["Score of no-model answers"] = f"{statistics.fmean(m):.0f}% of {len(m)}" if m else "-"
    return s


def print_table(table):
    names = list(table)
    keys = [k for k in dict.fromkeys(k for t in table.values() for k in t) if k != "Days"]
    print("| | " + " | ".join(n.split("-", 1)[0] for n in names) + " |")
    print("|---|" + "---|" * len(names))
    for k in keys:
        print(f"| {k} | " + " | ".join(str(table[n].get(k, "-")) for n in names) + " |")


def compare(days):
    """The same table, each cell holding one value per day, oldest first."""
    per_day = {d: {step: stats(*load(step, [d]), [d]) for step in STEPS if load(step, [d])[0]} for d in days}
    table = {}
    for step in STEPS:
        cells = [per_day[d].get(step) for d in days]
        if not any(cells):
            continue
        keys = dict.fromkeys(k for c in cells if c for k in c)
        table[step] = {k: " → ".join(str(c.get(k, "-")) if c else "-" for c in cells) for k in keys}
        # What the step has spent from day 1 to each day, the number a budget is judged by.
        spent, running = 0.0, []
        day1 = sum(r["cost"] for r in load(step, [1])[0])
        for d in days:
            rows = load(step, [d])[0]
            if step.startswith("step1") and not rows:
                # Step 1 runs on day 1 only and every day costs the same: day 1 times the days.
                running.append(f"${day1 * d:.4f} (day 1 x {d})")
                continue
            spent += sum(r["cost"] for r in rows)
            running.append(f"${spent:.4f}" if rows else "-")
        table[step] = {"Cost": table[step].pop("Cost"), "Cost so far": " → ".join(running), **table[step]}
    print("Days " + " → ".join(map(str, days)) + "\n")
    print_table(table)


if __name__ == "__main__":
    if sys.argv[1:2] == ["compare"]:
        compare([int(a) for a in sys.argv[2:]])
        sys.exit()
    days = [int(a) for a in sys.argv[1:]] or list(range(1, 8))
    table = {}
    for step in STEPS:
        rows, grades = load(step, days)
        if rows:
            table[step] = stats(rows, grades, days)
    if "step1-whole-handbook-sonnet" in table and len(days) > 1:
        day1 = sum(r["cost"] for r in load("step1-whole-handbook-sonnet", [1])[0])
        table["step1-whole-handbook-sonnet"]["Cost"] += f" (week: ${7 * day1:.2f})"
    print_table(table)
