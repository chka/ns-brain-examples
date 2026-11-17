"""Level 3, step one: turn the handbook into a brain, once.

The model reads the handbook one section at a time and writes it out as short facts, each
filed under one scope of brain.SCOPES. The handbook's FAQ goes in as known questions: the
title is the question, the body the answer. Those are the questions the bot can answer later
without calling the model at all. This costs one pass over the handbook, like building the
RAG index in Level 2, and is counted separately from the chats.
"""
import concurrent.futures as cf
import json
import re
import shutil
import subprocess
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from common import SHOP, add, chat, cost
from brain import BRAIN_DIR, ENV, RUN, SCOPES, ns, remember

PROMPT = """Turn this section of a shop's customer-service handbook into memories for the shop's
assistant. Reply with a JSON array only. Each item: {{"scope": one of {scopes}, "type": "decision"
for a policy, "constraint" for something the assistant must never do, "knowledge" for a plain fact,
"title": one self-contained sentence of 8 to 20 words that states the fact with its numbers,
"body": up to 60 words of detail, "question": true only for a question-and-answer pair from an
FAQ, whose title is then the customer's question and whose body is the full answer}}.
Keep every number, price and time limit. Skip anything that only repeats an earlier item.

SECTION:
{section}"""


def sections(markdown, max_words=1200):
    """Handbook sections, with long ones cut at their ### headings so each reply stays short.
    Every piece keeps its section title, so "Express: 2 days" still says which zone it is."""
    pieces = []
    for s in re.split(r"\n(?=## )", markdown):
        if not s.startswith("## "):
            continue
        title, *subs = re.split(r"\n(?=### )", s)
        piece = title
        for sub in subs:
            if len((piece + sub).split()) > max_words and piece != title:
                pieces.append(piece)
                piece = title.splitlines()[0] + "\n"
            piece += "\n" + sub
        pieces.append(piece)
    return pieces


def facts_of(section):
    text, usage = chat([{"role": "user", "content": PROMPT.format(scopes=list(SCOPES), section=section)}],
                       max_tokens=12000)
    items = json.loads(text[text.index("["):text.rindex("]") + 1])
    return items, usage


if __name__ == "__main__":
    shutil.rmtree(RUN, ignore_errors=True)
    (RUN / "config/brain").mkdir(parents=True)
    BRAIN_DIR.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=BRAIN_DIR, check=True)
    ns("init", "--project", "Kestrel Row", "--no-hook")

    total = {}
    with cf.ThreadPoolExecutor(6) as ex:
        results = list(ex.map(facts_of, sections((SHOP / "handbook.md").read_text())))
    stored = questions = 0
    for items, usage in results:
        add(total, usage)
        for it in items:
            scope = it["scope"] if it["scope"] in SCOPES else "global"
            mem_type = "knowledge" if it.get("question") else it.get("type", "knowledge")
            if mem_type not in ("decision", "constraint", "knowledge", "gotcha"):
                mem_type = "knowledge"
            # The handbook is curated by the shop, so it goes in as confirmed. Two facts with
            # similar titles are both kept here; for what the bot learns later the duplicate
            # guard stays on (see bot.py).
            if remember(scope, mem_type, it["title"], it["body"] or it["title"],
                        "--source", "shop/handbook.md", "--confidence", "confirmed", "--allow-duplicate")[0]:
                stored += 1
                questions += bool(it.get("question"))
    summary = {"memories": stored, "known_questions": questions, "tokens": total, "cost": round(cost(total), 4)}
    (RUN / "setup.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
