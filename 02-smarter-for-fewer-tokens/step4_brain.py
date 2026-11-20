"""Step 4: back to the strong model, with the handbook in ns-brain and the customer's purchases
beside it. Better answers than step 2, for less.

# What changes against step 2

Step 2 found passages by meaning alone and knew nothing about the person asking. Step 4 replaces
the passage index with a brain (see brain.py) and changes three things:

1. Facts instead of passages. The handbook was rewritten once into 484 short facts, each
   filed in a scope such as returns:sale or hours:holidays. A fact is one sentence with its
   numbers, so twelve facts carry what five 350-word passages carried, in a fraction of the
   tokens, and a fact about sale returns cannot drag in the size table that happened to share
   its passage.

2. A local classifier picks where to look. A small embedding model (all-MiniLM-L6-v2, 22 million
   parameters) runs on this machine and compares the question with every scope's description and
   with the titles filed in it. It picks the two most likely scopes in a few milliseconds, with
   no network call. The candidates are everything filed there plus ns-brain's own word matches
   from the scopes above, and the local model keeps the twelve closest in meaning.

3. The store knows its customer. The first time a customer logs in (by email), their purchases
   are copied from the store's order system into the brain, under a scope of their own,
   customer:<name>. A question about "the jacket I bought" is answered with that jacket's
   order, size, price and date in front of the model.

# What every call carries

The rules and the "core" facts (the scopes customers ask about most: hours, returns, exchanges,
payment...) are identical in every call, so they come first and are cached. After them comes
the conversation, then this question's facts and, when the question can use them, the
customer's purchases. Only that last part is new on each call.

# What it does not do yet

It never answers without the model and it learns nothing: every message is a model call, even
the tenth "what are your hours" of the day. That is step 5.

    python3 brain.py build              once, before any brain step
    python3 step4_brain.py 1
"""
import json
import re
import time

from sentence_transformers import SentenceTransformer

from brain import SCOPES, copy_fresh
from harness import RULES, SHOP, SONNET, chat, run

ORDERS = json.loads((SHOP / "orders.json").read_text())["customers"]

# How many facts beyond the core go to the model with a question.
FACTS = 12

# The scopes whose facts go into every call: what a store is asked most, and the rules an answer
# gets wrong when one is missing (the holiday window changes every return deadline in November).
CORE = ["hours", "hours:holidays", "returns", "returns:sale", "returns:holiday", "returns:receipt",
        "returns:never", "exchanges", "payment", "online", "alterations", "products"]

# A message about the customer's own purchases. Those get the customer's records in the prompt.
OWN = re.compile(r"\b(i bought|i got|i ordered|did i buy|last time|for me|recommend|"
                 r"receipt for the|do i have to return the|points do i have|is my|"
                 r"my (order|parcel|items?|stuff|size|points|account|hem|jeans|dress|shirts?|boots|jacket|return))\b"
                 r"|\bthe [a-z ]+ (is|are) too\b", re.I)

# A message that only makes sense after the one before it: "and sunday?"
FOLLOW_UP = re.compile(r"^\s*(and|also|but|what about|how about|then)\b", re.I)

ANSWERING = """- Answer from the store facts below and, for a question about the customer's own purchases, from
  what the store knows about the customer. Send the customer to the store team only when the facts do
  not cover the question.
- Your earlier replies in this chat were written from the facts shown with those questions, which are
  not repeated here. Treat them as correct and never take them back unless the customer disputes them."""


def alteration_title(order):
    """An alteration as one record, naming the altered item ("a hem on order NY-2460" alone left
    the model guessing which item of the order it was)."""
    a = order["alteration"]
    items = [it["item"].split(",")[0] for it in order["items"] if it["item"].startswith(("Harbour", "Inis"))]
    what = f"{a['what']} on the {' / '.join(items)}" if items else a["what"]
    return f"Alteration ({what}) on order {order['order']}, ready for pickup from {a['ready']}"


class BrainBot:
    """What the whole store shares: the brain, the local model and the core facts."""

    name, model = "step4-brain-sonnet", SONNET

    def __init__(self):
        self.brain = copy_fresh(self.name)
        self.prepare_brain()
        # The local model runs on the CPU here: it takes a few milliseconds a question either way.
        self.encoder = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2", device="cpu")
        self.vectors = {}
        # The classifier compares a question with each scope's description and with every title
        # filed in that scope, so it gets better as the brain grows.
        self.labels, texts = [], []
        for scope, description in SCOPES.items():
            for m in [{"title": description}] + self.brain.browse(scope):
                self.labels.append(scope)
                texts.append(m["title"])
        self.scope_vectors = self.encode(texts)
        core = [m for s in CORE for m in self.brain.browse(s) if not m["title"].rstrip().endswith("?")]
        self.core_ids = {m["id"] for m in core}
        self.core = "\n".join(f"- {self.fact_text(m, title_only=True)}" for m in core)

    def prepare_brain(self):
        """Anything to add to the brain before the first customer. Nothing at this step."""

    def start_chat(self, email, date):
        return Conversation(self, email, date)

    def encode(self, text):
        return self.encoder.encode(text, convert_to_tensor=True, normalize_embeddings=True)

    def classify(self, text):
        """The two scopes this text most likely belongs to, and the text's vector."""
        q = self.encode(text)
        best = {}
        for label, similarity in zip(self.labels, (self.scope_vectors @ q).tolist()):
            best[label] = max(best.get(label, 0), similarity)
        return sorted(best, key=best.get, reverse=True)[:2], q

    def vector(self, memory):
        """A memory's vector, computed once and kept."""
        if memory["id"] not in self.vectors:
            self.vectors[memory["id"]] = self.encode(f"{memory['title']}. {memory['body']}")
        return self.vectors[memory["id"]]

    def fact_text(self, memory, title_only=False):
        """How a fact is written into the prompt."""
        return memory["title"] if title_only else f"{memory['title']}. {memory['body']}"

    def facts_for(self, message, scopes, q):
        """The FACTS facts closest in meaning to the question, from the two scopes and from
        ns-brain's word matches in the scopes above them, leaving out the core (already sent)."""
        candidates = {}
        for s in scopes:
            for m in self.brain.browse(s, n=60) + self.brain.recall(message, s, n=8):
                if not m["scope"].startswith("customer") and m["id"] not in self.core_ids:
                    candidates[m["id"]] = m
        ranked = sorted(candidates.values(), key=lambda m: float(self.vector(m) @ q), reverse=True)[:FACTS]
        return [f"- [{m['scope']}] {self.fact_text(m)}" for m in ranked]


class Conversation:
    """One customer's chat. It holds what was said; everything else belongs to the store."""

    answering = ANSWERING

    def __init__(self, shop, email, date):
        self.shop, self.email, self.date = shop, email, date
        self.history, self.asked = [], []
        self.customer = "customer:" + re.sub(r"[^a-z]+", "-", email.split("@")[0].lower()).strip("-")
        self.name = ORDERS.get(email, {}).get("name")
        if email in ORDERS and not shop.brain.browse(self.customer):
            self.copy_orders()

    def copy_orders(self):
        """First login: the customer's purchases go from the order system into the brain, one
        memory per item, dated with the purchase date."""
        c, brain = ORDERS[self.email], self.shop.brain
        brain.remember(self.customer, "knowledge", f"Customer {c['name']}, logs in as {self.email}",
                       "A customer of the store: the purchases below come from the store system.",
                       "--source", "shop/orders.json", "--confidence", "confirmed")
        for o in c["orders"]:
            for it in o["items"]:
                brain.remember(self.customer, "knowledge",
                               f"Bought {it['item']}, size {it['size']}, ${it['price']}, {it['tag']}, "
                               f"order {o['order']} {o['where']} on {o['date']}",
                               f"Order {o['order']}, bought {o['where']} on {o['date']}.", "--date", o["date"],
                               "--source", "shop/orders.json", "--confidence", "confirmed", "--allow-duplicate")
            if "alteration" in o:
                brain.remember(self.customer, "knowledge", alteration_title(o),
                               "At the alterations counter, lower floor, during store hours.", "--date", o["date"],
                               "--source", "shop/orders.json", "--confidence", "confirmed", "--allow-duplicate")

    # The steps that follow change a message's journey by overriding the methods below. At this
    # step every message goes to the model.

    def question(self, message):
        """The message as it is classified and matched."""
        return message

    def without_model(self, message):
        """An answer that needs no model at all, or None. None at this step."""
        return None

    def from_memory(self, question, scopes, q):
        """A stored answer to a question the store has already answered, or None. None here."""
        return None

    def profile(self):
        """What the store knows about this customer, as prompt lines."""
        return "\n".join(f"- {r['title']}. {r['body']}" for r in self.shop.brain.browse(self.customer))

    def wants_profile(self, message, scopes):
        """Only questions that can use the purchases get them: on "how long does shipping take" a
        hem waiting for pickup only pulls the model off topic."""
        return bool(self.name and (OWN.search(message) or any(
            s.split(":")[0] in ("returns", "exchanges", "alterations", "rewards") for s in scopes)))

    def system(self):
        return f"{RULES}\n{self.answering}\n\nCORE STORE FACTS:\n{self.shop.core}"

    def prompt(self, message, facts, profile):
        previous = f"\nTHE CUSTOMER'S PREVIOUS MESSAGE: {self.asked[-2]}" if len(self.asked) > 1 else ""
        return (f"TODAY: {self.date}\n\nWHAT THE STORE KNOWS ABOUT THIS CUSTOMER:\n{profile or '- nothing needed'}"
                f"\n\nSTORE FACTS FOR THIS QUESTION:\n" + "\n".join(facts) + f"{previous}\n\nCUSTOMER: {message}")

    def finish(self, message, text, scopes):
        """Turn the model's reply into the answer, plus anything to report. Here it is the reply."""
        return text, {}

    def reply(self, message):
        started = time.perf_counter()
        self.asked.append(message)
        question = self.question(message)

        answer = self.without_model(message)
        if answer:
            return self.answered(message, answer, started, matched="code")

        follow_up = FOLLOW_UP.search(message) and len(self.asked) > 1
        scopes, q = self.shop.classify(" ".join(self.asked[-2:]) if follow_up else question)
        stored = self.from_memory(question, scopes, q)
        if stored:
            return self.answered(message, stored["answer"], started, scopes=scopes, matched=stored["matched"])
        local_ms = (time.perf_counter() - started) * 1000

        facts = self.shop.facts_for(message, scopes, q)
        profile = self.profile() if self.wants_profile(message, scopes) else ""
        # The system prompt is the same in every call and is marked for caching; the history holds
        # only what the customer and the store said, never the facts sent with earlier messages.
        # Without the last answering rule the model, seeing an earlier claim with no fact behind it
        # in this call, took back correct answers ("I had no basis to promise a student discount").
        messages = [{"role": "system", "content": self.system(), "cache_control": {"type": "ephemeral"}}]
        messages += self.history + [{"role": "user", "content": self.prompt(message, facts, profile)}]
        text, usage = chat(messages, self.shop.model)
        answer, extra = self.finish(message, text, scopes)
        self.history += [{"role": "user", "content": message}, {"role": "assistant", "content": answer}]
        return answer, {"usage": usage, "path": "brain+llm", "scopes": scopes, "facts": len(facts),
                        "profile": bool(profile), "local_ms": round(local_ms, 1), **extra}

    def answered(self, message, answer, started, **info):
        """Bookkeeping for an answer that never reached the model."""
        self.history += [{"role": "user", "content": message}, {"role": "assistant", "content": answer}]
        return answer, {"usage": {}, "path": "memory", "local_ms": round((time.perf_counter() - started) * 1000, 1),
                        **info}


if __name__ == "__main__":
    bot = BrainBot()
    run(bot, bot.name)
