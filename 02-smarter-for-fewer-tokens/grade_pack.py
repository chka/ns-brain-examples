"""grade_pack.py prepares a day's answers for the judge and merges the judge's scores back.

# Why the answers are judged together

Graded one step at a time, two judges could read the same question differently (one called
"how many days do I have to return" personal, the other general) and the steps' scores then
differed by the judge, not by the bot. So the judge now sees one customer message at a time with
every version's answer to it side by side and scores each from 0 to 100. Every step is measured
against the same reading of the same question, and the judge can see what a complete answer to
it looks like.

# Why the labels are shuffled

The answers are labelled A, B, C... in a different random order for every question, and the
key that maps labels back to steps stays out of the pack. A judge who knew that answer D always
came from the expensive model, or from the step the article is rooting for, could be swayed by
it; one who sees only anonymous answers cannot.

    python3 grade_pack.py pack 1        writes results/judge/day1.pack-N.json and day1.key.json
    python3 grade_pack.py merge 1       reads results/judge/day1.scores-N.jsonl, writes the grades
"""
import json
import random
import sys

from harness import RESULTS, SHOP
from report import STEPS

JUDGE = RESULTS / "judge"
PARTS = 3                       # the day is split between this many judges, whole questions each
LABELS = "ABCDEFGHIJ"
ORDERS = json.loads((SHOP / "orders.json").read_text())["customers"]


def answers(step, day):
    path = RESULTS / step / f"day{day}.jsonl"
    return {(r["chat"], r["turn"]): r for r in map(json.loads, path.open())}


def pack(day):
    JUDGE.mkdir(parents=True, exist_ok=True)
    by_step = {s: answers(s, day) for s in STEPS if (RESULTS / s / f"day{day}.jsonl").exists()}
    # Step 1 runs on day 1 only, so later days are judged across the six other steps.
    first = next(iter(by_step.values()))
    rng = random.Random(f"kestrel-row-day{day}")
    items, key = [], {}
    for (chat, turn), row in first.items():
        steps = list(by_step)
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
    size = -(-len(items) // PARTS)
    for n in range(PARTS):
        part = items[n * size:(n + 1) * size]
        (JUDGE / f"day{day}.pack-{n + 1}.json").write_text(json.dumps(part, indent=1))
        print(f"day{day}.pack-{n + 1}.json: {len(part)} questions x {len(by_step)} answers")


def merge(day):
    key = json.loads((JUDGE / f"day{day}.key.json").read_text())
    scores = {}
    for n in range(PARTS):
        for line in (JUDGE / f"day{day}.scores-{n + 1}.jsonl").open():
            s = json.loads(line)
            scores[s["id"]] = s
    missing = set(key) - set(scores)
    if missing:
        sys.exit(f"the judge did not score: {sorted(missing)}")
    steps = {step for labels in key.values() for step in labels.values()}
    for step in steps:
        rows = answers(step, day)
        with (RESULTS / step / f"day{day}.grades.jsonl").open("w") as out:
            for (chat, turn) in rows:
                s = scores[f"{chat}/{turn}"]
                label = next(l for l, st in key[f"{chat}/{turn}"].items() if st == step)
                a = s["answers"][label]
                out.write(json.dumps({"chat": chat, "turn": turn, "kind": s["kind"], "score": a["score"],
                                      "knew_customer": a.get("knew_customer"), "note": a.get("note", "")}) + "\n")
    print(f"merged {len(scores)} questions into {len(steps)} steps")


if __name__ == "__main__":
    {"pack": pack, "merge": merge}[sys.argv[1]](int(sys.argv[2]))
