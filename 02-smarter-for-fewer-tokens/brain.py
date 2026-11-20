"""brain.py is how steps 4 to 7 talk to ns-brain, and how the store's brain is built once.

# What ns-brain is here

ns-brain (https://ns-brain.fightclub.pro) is a memory store for programs and agents: one binary,
a SQLite file per project. A memory is a short title (one sentence that carries the fact),
a body with the detail, a type (knowledge, decision, constraint, preference), a source and a
confidence, and a scope: the place it is filed. Scopes nest with colons, so a search in
returns:sale also sees returns and global, but never returns:faulty or online.

# Why talk to it through the command line

The bot runs `ns-brain <command> --json` and reads the answer. That is the interface any
program in any language has, with nothing to link against, so the code below is the whole
integration. Every path ns-brain writes to is pinned inside this step's own folder under run/,
so the example never reads or writes a brain of yours, and two steps never share one.

# Building the store's brain (python3 brain.py build)

A model reads the handbook one section at a time and writes it out as short facts, each filed
under one scope of SCOPES below. The handbook's FAQ goes in as known questions: the title is the
customer's question and the body is the answer. This is a one-off pass, the brain's equivalent
of step 2's index, and its cost is reported apart from the chats. Every brain step starts from
a copy of this one brain, so they start from the same facts.
"""
import concurrent.futures as cf
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
RUN = HERE / "run"
FRESH = RUN / "fresh"           # the brain as built from the handbook, before any customer

# Where each kind of fact lives. The description is what the local classifier compares a question
# with before any fact has been filed there (see step4_brain.py).
SCOPES = {
    "global": "who the store is, phone and email, what the assistant may and may not do",
    "store": "the store's address, location, subway, parking, accessibility, restroom, wifi, dogs",
    "hours": "opening hours, what time the store opens or closes, late night, Sunday hours",
    "hours:holidays": "holiday hours, Thanksgiving, Black Friday, Christmas, New Year",
    "returns": "return policy, how many days to return, how to return, refunds and when they arrive",
    "returns:receipt": "returns without a receipt, lost receipt, store credit, ID",
    "returns:sale": "returning sale items, final sale, store credit",
    "returns:never": "items that can never be returned: underwear, swimwear, earrings, gift cards, altered items",
    "returns:holiday": "holiday returns, purchases in November and December returned until January 15",
    "returns:online": "returning online orders in store or by mail, prepaid label",
    "returns:faulty": "faulty items, defects, repair or replacement",
    "exchanges": "exchanging for a different size or color, exchange policy",
    "payment": "payment methods, Apple Pay, Amex, cash, Klarna, installments, Venmo, checks",
    "tax": "sales tax in New York City, clothing tax exemption, tax refund for tourists",
    "gift-cards": "gift cards, amounts, expiry, balance, e-gift",
    "alterations": "alterations, hemming, prices, how long alterations take, pickup",
    "services": "gift wrapping, personal styling, holds, stock checks",
    "online": "shipping, delivery times and costs, same-day delivery, buy online pick up in store, order changes",
    "rewards": "Row Rewards points, rewards, birthday code",
    "discounts": "student discount, military discount, price matching, price adjustment, Black Friday sale",
    "products": "product lines, prices, fabrics, colors, fit notes, sizes",
    "care": "washing and care instructions for merino, linen, denim, waxed cotton, leather",
}


class Brain:
    """One ns-brain store, in its own folder: run/<name>/ holds the project, the config and the DB."""

    def __init__(self, folder):
        self.folder = pathlib.Path(folder)
        self.project = self.folder / "kestrel-row"
        # Anything inherited from the caller's environment that would point ns-brain at another
        # brain (the CLAUDE_*, BRAIN_* and NS_BRAIN_* variables) is dropped, then every path is
        # pinned inside this folder.
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "BRAIN", "NS_BRAIN"))}
        self.env.update(CLAUDE_CONFIG_DIR=str(self.folder / "config"),
                        BRAIN_GLOBAL_DB=str(self.folder / "config/brain/brain.db"),
                        NS_BRAIN_CREDENTIALS=str(self.folder / "no-credentials"),
                        BRAIN_HOME=str(self.project / "brain"))

    def ns(self, *args, check=True):
        """Run one ns-brain command in this brain and return the finished process."""
        r = subprocess.run(["ns-brain", *args], env=self.env, cwd=self.project, capture_output=True, text=True)
        if check and r.returncode:
            raise RuntimeError(f"ns-brain {args[0]}: {r.stderr.strip() or r.stdout.strip()}")
        return r

    def remember(self, scope, kind, title, body, *extra):
        """Store one memory. Returns (id, None) when stored, or (None, existing id) when ns-brain
        refused it because the scope already holds a memory about the same thing. ns-brain makes
        that check itself on every write, which is what keeps a learning bot from filling the
        brain with twenty wordings of one answer."""
        r = self.ns("remember", "--json", "--scope", scope, "--type", kind, "--title", title,
                    "--body", body, *extra, check=False)
        if r.returncode == 0:
            return json.loads(r.stdout)["id"], None
        m = re.search(r"#(\d+) (?:already holds|in \S+ is already about)", r.stderr + r.stdout)
        return None, int(m.group(1)) if m else None

    def get(self, memory_id):
        return json.loads(self.ns("get", "--json", str(memory_id)).stdout)

    def recall(self, query, scope, n=8):
        """The memories in this scope and the scopes above it that hold the query's words, best
        first. A miss returns nothing: ns-brain never answers a miss with the whole brain."""
        r = self.ns("recall", "--json", "--no-touch", "--scope", scope, "--n", str(n), query)
        return json.loads(r.stdout)["results"]

    def browse(self, scope, n=200):
        """Everything filed in exactly this scope."""
        r = self.ns("recall", "--json", "--no-touch", "--browse", "--scope", scope, "--n", str(n), "")
        return [m for m in json.loads(r.stdout)["results"] if m["scope"] == scope]


def copy_fresh(name):
    """A step's own brain: a copy of the fresh one, made the first time the step runs."""
    folder = RUN / name
    if not folder.exists():
        if not FRESH.exists():
            sys.exit("no brain yet: run python3 brain.py build first")
        shutil.copytree(FRESH, folder, symlinks=True)
    return Brain(folder)


BUILD_PROMPT = """Turn this section of a store's customer-service handbook into memories for the
store's assistant. Reply with a JSON array only. Each item: {{"scope": one of {scopes}, "type":
"decision" for a policy, "constraint" for something the assistant must never do, "knowledge" for a
plain fact, "title": one self-contained sentence of 8 to 20 words that states the fact with its
numbers, "body": up to 60 words of detail, "question": true only for a question-and-answer pair
from an FAQ, whose title is then the customer's question and whose body is the full answer}}.
Keep every number, price and time limit. Skip anything that only repeats an earlier item.

SECTION:
{section}"""


def sections(markdown, max_words=1200):
    """Handbook sections, long ones cut at their ### headings so each model reply stays short.
    Every piece keeps its section title."""
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


def build():
    """Build run/fresh from shop/handbook.md (see the module comment)."""
    from harness import SHOP, SONNET, add, chat, cost

    def facts_of(section):
        text, usage = chat([{"role": "user", "content": BUILD_PROMPT.format(scopes=list(SCOPES), section=section)}],
                           SONNET, max_tokens=12000)
        return json.loads(text[text.index("["):text.rindex("]") + 1]), usage

    shutil.rmtree(FRESH, ignore_errors=True)
    brain = Brain(FRESH)
    (FRESH / "config/brain").mkdir(parents=True)
    brain.project.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=brain.project, check=True)
    brain.ns("init", "--project", "Kestrel Row", "--no-hook")
    total, stored, questions = {}, 0, 0
    with cf.ThreadPoolExecutor(6) as pool:
        results = list(pool.map(facts_of, sections((SHOP / "handbook.md").read_text())))
    for items, usage in results:
        add(total, usage)
        for it in items:
            scope = it["scope"] if it["scope"] in SCOPES else "global"
            kind = "knowledge" if it.get("question") else it.get("type", "knowledge")
            if kind not in ("decision", "constraint", "knowledge"):
                kind = "knowledge"
            # The handbook is the store's own text, so it goes in as confirmed, and two facts with
            # similar titles are both kept. What the bot learns later keeps the duplicate guard.
            if brain.remember(scope, kind, it["title"], it["body"] or it["title"],
                              "--source", "shop/handbook.md", "--confidence", "confirmed", "--allow-duplicate")[0]:
                stored += 1
                questions += bool(it.get("question"))
    summary = {"memories": stored, "known_questions": questions, "tokens": total, "cost": round(cost(total, SONNET), 4)}
    (RUN / "build.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__" and sys.argv[1:] == ["build"]:
    build()
