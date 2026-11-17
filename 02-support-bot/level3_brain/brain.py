"""A thin wrapper around the ns-brain command line, plus the shop's scope tree.

The bot talks to ns-brain the way any program can: by running the binary and reading
--json. Every path ns-brain writes to is pinned inside run/, so this example never touches
a brain of your own.
"""
import json
import os
import pathlib
import re
import subprocess

HERE = pathlib.Path(__file__).resolve().parent
# Each bot keeps its own brain: BRAIN_RUN=run-haiku and BRAIN_RUN=run-sonnet never share one.
RUN = HERE.parent / os.environ.get("BRAIN_RUN", "run")
BRAIN_DIR = RUN / "kestrel-row"

ENV = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "BRAIN", "NS_BRAIN"))}
ENV.update(CLAUDE_CONFIG_DIR=str(RUN / "config"), BRAIN_GLOBAL_DB=str(RUN / "config/brain/brain.db"),
           NS_BRAIN_CREDENTIALS=str(RUN / "no-credentials"), BRAIN_HOME=str(BRAIN_DIR / "brain"))

# Where each kind of fact lives. A scope nests with colons: a search in returns:sale also
# sees returns and global, but never returns:faulty or delivery. The descriptions are what
# the local classifier compares a question with, before any fact has been learned.
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


def ns(*args, check=True):
    r = subprocess.run(["ns-brain", *args], env=ENV, cwd=BRAIN_DIR, capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f"ns-brain {args[0]}: {r.stderr.strip() or r.stdout.strip()}")
    return r


def remember(scope, mem_type, title, body, *extra):
    """Store one memory. Returns (id, None), or (None, existing id) when ns-brain refused it
    because the scope already holds a memory about the same thing, or (None, None) for any
    other refusal."""
    r = ns("remember", "--json", "--scope", scope, "--type", mem_type, "--title", title,
           "--body", body, *extra, check=False)
    if r.returncode == 0:
        return json.loads(r.stdout)["id"], None
    m = re.search(r"#(\d+) (?:already holds|in \S+ is already about)", r.stderr + r.stdout)
    return None, int(m.group(1)) if m else None


def get(mem_id):
    return json.loads(ns("get", "--json", str(mem_id)).stdout)


def recall(query, scope, n=8):
    """The memories in this scope (and the scopes above it) that hold the question's words,
    best first. Empty on a miss: ns-brain never answers a miss with the whole brain."""
    r = ns("recall", "--json", "--no-touch", "--scope", scope, "--n", str(n), query)
    return json.loads(r.stdout)["results"]


def browse(scope, n=200):
    """Everything filed in a scope, for when the question's words matched nothing."""
    r = ns("recall", "--json", "--no-touch", "--browse", "--scope", scope, "--n", str(n), "")
    return [m for m in json.loads(r.stdout)["results"] if m["scope"] == scope]
