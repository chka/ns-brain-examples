"""grade_models.py judges step 7 on Haiku against step 7 on a cheaper model, side by side.

# Why a separate judging round

The main week was judged with every step's answer to a message side by side (grade_pack.py).
This round asks a narrower question, so the judge sees only the answers of step 7 on Haiku and on
the cheaper model beside it. Labels are shuffled per message exactly as in
grade_pack.py, and the same instructions apply (results/JUDGING.md).

Its packs and scores live in results/judge-models/ and its grades go to
results/<step>/dayN.grades-models.jsonl, so the main week's judgement is never overwritten. Step 7
on Haiku is judged again here rather than reusing its old scores: a score is only comparable with
the answers it was read beside.

    python3 grade_models.py pack 1       writes results/judge-models/day1.pack.json and day1.key.json
    python3 grade_models.py merge 1      reads results/judge-models/day1.scores.jsonl
"""
import json
import random
import sys

from grade_pack import LABELS, ORDERS, answers
from harness import RESULTS

JUDGE = RESULTS / "judge-models"
# Qwen3.7 Flash was dropped after day 1: it reasons before every reply, which made its median
# model reply 16 seconds, too slow for a customer chat whatever it costs.
STEPS = ["step7-tuned-haiku", "step7g-tuned-gemma"]


def pack(day):
    JUDGE.mkdir(parents=True, exist_ok=True)
    by_step = {s: answers(s, day) for s in STEPS}
    first = by_step[STEPS[0]]
    for s, rows in by_step.items():
        if set(rows) != set(first):
            sys.exit(f"{s} day {day} does not have the same messages as {STEPS[0]}")
    rng = random.Random(f"kestrel-row-models-day{day}")
    items, key = [], {}
    for (chat, turn), row in first.items():
        steps = list(STEPS)
        rng.shuffle(steps)
        labels = dict(zip(LABELS, steps))
        key[f"{chat}/{turn}"] = labels
        earlier = [r["question"] for (c, t), r in first.items() if c == chat and t < turn]
        customer = ORDERS.get(row["email"])
        items.append({"id": f"{chat}/{turn}", "date": row["date"], "customer": row["email"],
                      "orders": customer["orders"] if customer else "guest: not logged in",
                      "earlier_in_this_chat": earlier, "question": row["question"],
                      "answers": {label: by_step[step][(chat, turn)]["answer"] for label, step in labels.items()}})
    (JUDGE / f"day{day}.key.json").write_text(json.dumps(key, indent=1))
    (JUDGE / f"day{day}.pack.json").write_text(json.dumps(items, indent=1))
    print(f"day{day}.pack.json: {len(items)} questions x {len(STEPS)} answers")


def merge(day):
    key = json.loads((JUDGE / f"day{day}.key.json").read_text())
    scores = {s["id"]: s for s in map(json.loads, (JUDGE / f"day{day}.scores.jsonl").open())}
    missing = set(key) - set(scores)
    if missing:
        sys.exit(f"the judge did not score: {sorted(missing)}")
    for step in STEPS:
        with (RESULTS / step / f"day{day}.grades-models.jsonl").open("w") as out:
            for (chat, turn) in answers(step, day):
                s = scores[f"{chat}/{turn}"]
                label = next(l for l, st in key[f"{chat}/{turn}"].items() if st == step)
                a = s["answers"][label]
                out.write(json.dumps({"chat": chat, "turn": turn, "kind": s["kind"], "score": a["score"],
                                      "knew_customer": a.get("knew_customer"), "note": a.get("note", "")}) + "\n")
    print(f"merged {len(scores)} questions into {len(STEPS)} steps")


if __name__ == "__main__":
    {"pack": pack, "merge": merge}[sys.argv[1]](int(sys.argv[2]))
