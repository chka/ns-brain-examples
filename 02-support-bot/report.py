"""One table comparing the bots over the days that have run, from results/<bot>/dayN.jsonl and,
when they exist, the grades in results/<bot>/dayN.grades.jsonl (see results/GRADING.md).

python3 report.py            every day that every bot has finished
python3 report.py 1 2        only those days
"""
import json
import os
import pathlib
import statistics
import sys

RESULTS = pathlib.Path(__file__).resolve().parent / "results"
BOTS = os.environ.get("BOTS", "level2-sonnet level2-haiku level3-sonnet level3-haiku").split()


def load(bot, days):
    rows, grades = [], []
    for d in days:
        rows += [json.loads(l) for l in (RESULTS / bot / f"day{d}.jsonl").open()]
        g = RESULTS / bot / f"day{d}.grades.jsonl"
        grades += [json.loads(l) for l in g.open()] if g.exists() else []
    return rows, grades


def pct(n, total):
    return f"{n} ({100 * n / total:.0f}%)" if total else "-"


def stats(rows, grades):
    s = {}
    n = len(rows)
    memory = [r for r in rows if r["path"] == "memory"]
    llm = [r for r in rows if r["path"] != "memory"]
    cost = sum(r["cost"] for r in rows)
    t = [r["seconds"] for r in rows]
    tin = sum(r["usage"].get("input", 0) + r["usage"].get("cache_write", 0) + r["usage"].get("cache_read", 0) for r in llm)
    tout = sum(r["usage"].get("output", 0) for r in llm)
    s["Messages"] = n
    s["Cost"] = f"${cost:.4f}"
    s["Cost per message"] = f"{100 * cost / n:.3f} cents"
    s["Cost per 1,000 messages"] = f"${1000 * cost / n:.2f}"
    s["LLM calls"] = sum(r["llm_calls"] for r in rows)
    s["Answered from memory (no LLM)"] = pct(len(memory), n)
    s["Reply time avg / median"] = f"{statistics.fmean(t):.2f} / {statistics.median(t):.2f} s"
    s["Reply time min / max"] = f"{min(t):.2f} / {max(t):.2f} s"
    s["Reply time p95"] = f"{sorted(t)[int(0.95 * (n - 1))]:.2f} s"
    s["Memory answers avg time"] = f"{statistics.fmean(r['seconds'] for r in memory):.3f} s" if memory else "-"
    s["LLM answers avg time"] = f"{statistics.fmean(r['seconds'] for r in llm):.2f} s" if llm else "-"
    s["Waiting for the LLM, avg per LLM answer"] = f"{statistics.fmean(r['llm_seconds'] for r in llm):.2f} s" if llm else "-"
    s["Local work avg per message"] = f"{1000 * statistics.fmean(r['local_seconds'] for r in rows):.0f} ms"
    s["CPU per message avg (bot + ns-brain)"] = f"{1000 * statistics.fmean(r['cpu_seconds'] + r['ns_brain_cpu_seconds'] for r in rows):.0f} ms"
    s["CPU total"] = f"{sum(r['cpu_seconds'] + r['ns_brain_cpu_seconds'] for r in rows):.1f} s"
    s["Tokens in per LLM call"] = f"{tin / len(llm):,.0f}" if llm else "-"
    s["Tokens out per LLM call"] = f"{tout / len(llm):,.0f}" if llm else "-"
    if grades:
        sc = [g["score"] for g in grades]
        s["Graded"] = len(grades)
        s["Correct"] = pct(sc.count(2), len(sc))
        s["Partly right"] = pct(sc.count(1), len(sc))
        s["Wrong"] = pct(sc.count(0), len(sc))
        s["Smart score (points / max)"] = f"{sum(sc)}/{2 * len(sc)} ({100 * sum(sc) / (2 * len(sc)):.0f}%)"
        for kind in ("general", "personal", "follow-up", "rare"):
            k = [g["score"] for g in grades if g["kind"] == kind]
            s[f"Smart score, {kind} questions"] = f"{100 * sum(k) / (2 * len(k)):.0f}% of {len(k)}" if k else "-"
        personal = [g for g in grades if g["kind"] == "personal"]
        s["Knew the customer (personal questions)"] = pct(sum(1 for g in personal if g["knew_customer"]), len(personal))
        mem_ids = {(r["chat"], r["turn"]) for r in memory}
        mg = [g["score"] for g in grades if (g["chat"], g["turn"]) in mem_ids]
        s["Smart score of memory answers"] = f"{100 * sum(mg) / (2 * len(mg)):.0f}% of {len(mg)}" if mg else "-"
    return s


if __name__ == "__main__":
    days = [int(a) for a in sys.argv[1:]] or [d for d in range(1, 8)
                                            if all((RESULTS / b / f"day{d}.jsonl").exists() for b in BOTS)]
    table = {b: stats(*load(b, days)) for b in BOTS}
    keys = list(dict.fromkeys(k for s in table.values() for k in s))
    print(f"Days {', '.join(map(str, days))}\n")
    print("| | " + " | ".join(BOTS) + " |")
    print("|---|" + "---|" * len(BOTS))
    for k in keys:
        print(f"| {k} | " + " | ".join(str(table[b].get(k, "-")) for b in BOTS) + " |")
