"""Level 3, the lean expert: ns-brain instead of RAG, a local classifier, answers from memory
and a shop that remembers its customers.

For every message, in this order:

1. Login. The first time a customer logs in (email only), their orders are copied from the
   shop's order system into the brain, under a scope of their own: customer:<name>. Whatever
   the bot learns about them in chat (sizes, likes, what they plan to buy) goes there too.
2. Classify, locally. A small embedding model (all-MiniLM-L6-v2, 22M parameters) runs on this
   machine's GPU, or its CPU, and decides which scope of the brain the question belongs to:
   delivery:uk, returns:sale, sizes:men... No LLM call, no network.
3. Answer from memory. If the question means the same as one the shop already answered (an
   FAQ, or something this bot answered before) the stored answer is sent as it is. No LLM.
4. Otherwise ask the LLM, giving it only the facts ns-brain recalls from that scope (and the
   scopes above it) plus what the brain holds about this customer.
5. Teach. The LLM also says whether its answer would be the same for any customer. If so, the
   question and answer are stored as a known question, marked 'reported' (written by the bot,
   not checked by a person) so the shop can review them. Facts about the customer are stored
   as their preferences.
"""
import datetime
import json
import os
import re
import sys
import threading
import time
import pathlib

import torch
from sentence_transformers import CrossEncoder, SentenceTransformer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from common import RULES, SHOP, chat, run
from brain import RUN, SCOPES, browse, get, ns, recall, remember

ORDERS = json.loads((SHOP / "orders.json").read_text())["customers"]

# The model that answers new questions. Level 3 hands it about twenty facts picked for the
# question and what the shop knows about the customer, so a small, cheap model is enough;
# Levels 1 and 2 run Sonnet 5.5, which costs twenty times as much per token.
ANSWER_MODEL = os.environ.get("LEVEL3_MODEL", "fc:anthropic/claude-haiku-5.5")
PHRASINGS = RUN / "phrasings.json"

# When is a message "the same question" as one the shop already answered? Two local models
# must agree, calibrated on this shop's traffic (see the README):
# - the small embedding model puts the known question among its nearest, similarity >= 0.70;
# - a cross-encoder (bge-reranker-base), which reads both questions together, scores >= 0.85.
# Either one alone matched "return policy on jumpers" to "can I return my sale jumper".
NEAR, SAME = 0.70, 0.85
OWNER_SAME = 0.80   # an owner-written answer wins at this cross-encoder score

# How many facts beyond the core go to the LLM with a question, ranked by the local model.
FACTS = 12

# Messages about this customer's own orders always go to the LLM, and so do messages that
# need the previous one to make sense: a stored answer cannot know which jumper "it" is. A
# pronoun only counts when there IS a previous message ("how much does it cost?" opening a
# chat is a general question).
OWN = re.compile(r"\b(i bought|i got|i ordered|did i buy|i posted|last time|for me|recommend|"
                 r"receipt for the|do i have to return the|points do i have|is my|"
                 r"my (order|parcel|items?|stuff|size|points|account|hem|jeans|dress|shirts?|boots|jacket|return))\b"
                 r"|\bthe [a-z ]+ (is|are) too\b", re.I)
FOLLOW_UP = re.compile(r"^\s*(and|also|but|what about|how about|then)\b", re.I)
PRONOUN = re.compile(r"\b(it|them|that one|this one|these|those)\b", re.I)

# The scopes whose facts go into every call, before anything else: the questions a store gets
# most, and the rules an answer gets wrong when one of them is missing (the holiday window
# changes every return deadline in November). Being the same in every call, they are cached.
CORE = ["hours", "hours:holidays", "returns", "returns:sale", "returns:holiday", "returns:receipt",
        "returns:never", "exchanges", "payment", "online", "alterations", "products"]

HOURS = json.loads((SHOP / "hours.json").read_text())
_curated = json.loads((SHOP / "corrections.json").read_text())
CORRECTIONS = _curated["facts"]
CURATED = _curated["answers"]

# Words customers use that the store's facts do not: expanded before anything is matched.
GLOSSARY = {r"\bbopis\b": "buy online pick up in store", r"\bpersonal shopp(ing|er)\b": "personal styling appointment",
            r"\bstylist\b": "personal styling appointment", r"\bcurbside\b": "store pickup"}

# A sentence in a fact that tells staff what to do ("Mention that...", "If a customer asks...") is
# taken out before the fact reaches the model, which otherwise repeats it to the customer.
STAFF_SENTENCE = re.compile(r"(?:^|(?<=[.!?]\s))(?:mention|tell|explain|remind|ask|offer|direct|refer|send|pass|"
                            r"if a customer|if the customer|don't guess|do not guess)[^.!?]*[.!?]", re.I)


def expand(text):
    for pattern, words in GLOSSARY.items():
        text = re.sub(pattern, words, text, flags=re.I)
    return text


def clean_fact(text):
    return STAFF_SENTENCE.sub("", text).strip()


def hours_answer(message, date, after_hours=False):
    """Opening hours from the store's system, today's line first. No model: an hours question
    is a look-up, and a look-up cannot misread Thursday's late night."""
    m = message.lower()
    holiday = re.search(r"thanksgiving|black friday|christmas|new year|holiday", m)
    weekday_only = re.sub(r"black friday", "", m)   # "black friday" is a holiday, not a Friday
    asks_hours = re.search(r"\b(hours?|open|close|closing|late)\b", m) or (
        after_hours and re.search(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|weekend)s?\b", weekday_only))
    if not asks_hours and (not holiday or re.search(r"\b(sale|discount|deal|offer|off)\b", m)):
        return None
    today = datetime.date.fromisoformat(date)
    special = HOURS["special"].get(today.isoformat())
    day = today.strftime("%A")
    hours = special[1] if special else HOURS["regular"][day]
    todays = ("closed" if hours == "closed" else f"open {hours}") + (f" ({special[0]})" if special else "")
    week = "; ".join(f"{d} {h}" for d, h in HOURS["regular"].items())
    coming = [f"{name} ({datetime.date.fromisoformat(d).strftime('%a %b %d')}): {h}"
              for d, (name, h) in HOURS["special"].items()
              if today <= datetime.date.fromisoformat(d) <= today + datetime.timedelta(days=45)]
    asked = []
    for name in HOURS["regular"]:
        if re.search(rf"\b{name.lower()}s?\b", weekday_only) and name != day:
            # "and thursday?" means the coming Thursday, which may be a holiday: that date comes
            # first. On a Sunday the bot once led with regular Thursday hours the week of Thanksgiving.
            ahead = (list(HOURS["regular"]).index(name) - today.weekday()) % 7
            nxt = today + datetime.timedelta(days=ahead)
            if nxt.isoformat() in HOURS["special"]:
                hol, h = HOURS["special"][nxt.isoformat()]
                asked.append(f"This {name} ({nxt.strftime('%b %d')}) is {hol}: "
                             + ("we're closed" if h == "closed" else f"open {h}")
                             + f". Other {name}s: {HOURS['regular'][name]}")
            else:
                asked.append(f"{name}: {HOURS['regular'][name]}")
    for d, (name, h) in HOURS["special"].items():
        if holiday and holiday.group(0) in name.lower():
            asked.append(f"{name} ({datetime.date.fromisoformat(d).strftime('%a %b %d')}): {h}")
    if re.search(r"\blate\b", m):
        asked.insert(0, "Thursday is our late night: 10am-9pm")
        late = [f"{name} ({datetime.date.fromisoformat(d).strftime('%a %b %d')}) {h}"
                for d, (name, h) in HOURS["special"].items()
                if today <= datetime.date.fromisoformat(d) <= today + datetime.timedelta(days=45)
                and re.search(r"\b(9|10)pm", h)]
        if late:
            asked.insert(1, "Also open late: " + ", ".join(late))
    text = (" ".join(a + "." for a in asked) + " " if asked else "") + f"Today ({day}) we're {todays}. Regular hours: {week}."
    if coming:
        text += " Holiday hours: " + "; ".join(coming) + "."
    return text + " " + HOURS["note"]

# An answer that talks about the person or the conversation is not a store answer and is never
# stored for everyone ("since you asked about parking..." reached a different customer once).
PERSONAL_ANSWER = re.compile(r"\b(today|tonight|tomorrow|this (monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
                             r"you asked|since you|you mentioned|you said|you told|as we discussed|"
                             r"your (order|purchase|receipt|account|points|hem|size|jacket|jeans|shirt|dress|boots))\b", re.I)

# How Level 3 answers, on top of the RULES every level shares. Each line is here because an
# answer broke it during the test week.
ANSWERING = """- Answer from the store facts and what the store knows about the customer. Send the customer to the store
  team only when the facts do not cover the question.
- When a fact has an exception or a holiday rule (sale items, purchases from Nov 1 to Dec 24 returnable until
  Jan 15), say it.
- For a question about the customer's own purchases, use their purchases below: name each item and say whether
  and until when it can be returned or exchanged.
- The facts are notes written for staff. Never copy an instruction from them ("mention...", "explain...",
  "if a customer..."): say the thing itself to the customer.
- Use today's date and weekday exactly as given.
- Never state a limit or exclusion the facts do not state.
- The product lines in the facts are the store's whole range. Asked for something outside it (suits, kids'
  shoes, perfume), say plainly the store doesn't carry it and name the closest line it does carry, if any.
- An online order's window counts from delivery; if the delivery date is not known, give 30 days after the
  purchase as the earliest it ends ("until at least ...").
- When a customer asks to exchange their own item, give the exchange deadline and the return deadline.
- Say how to return: in store, or for an online order by mail with the prepaid label in the box. Never send the
  customer to the team to arrange a return they can make themselves."""

FORMAT = """Reply with one JSON object and nothing else, short:
{"answer": "<your reply, at most 70 words>",
 "general_question": "<if the answer is the same for anyone (a store policy, hours, payment, care...), the question as a short self-contained question; else null>",
 "customer_notes": [<only facts this customer just told you about themselves, or a size you recommended; usually empty>]}
For hours give the regular week and the holiday hours, never only today's, so the answer stays true tomorrow."""


# Handbook lines written for staff ("If a customer wants...", "explain the options") are fine as
# facts for the model, but must never be sent to a customer as a stored answer.
STAFF_TEXT = re.compile(r"\b(if a customer|the customer|customers who|tell the customer|let the customer|"
                        r"explain (the|that|to)|the assistant|staff should|refer (them|the)|direct (them|the))\b", re.I)
ORDER_NUMBER = re.compile(r"\bNY-\d+\b")


def normalise(text):
    return re.sub(r"[^a-z0-9 ]+", "", text.lower()).strip()


# Words that narrow a question. A stored question carrying one the customer did not use answers
# a narrower question: "what is your return policy on sale items?" is not "return policy".
QUALIFIERS = {"sale", "online", "holiday", "holidays", "christmas", "thanksgiving", "black", "friday",
              "sunday", "saturday", "thursday", "receipt", "gift", "student", "military", "faulty", "final",
              "altered", "alteration", "tax", "express", "same-day", "kids", "canada", "california", "klarna", "weekend",
              "venmo", "amex", "apple", "google", "cash", "merino", "linen", "denim", "leather", "parking", "subway"}


def qualifiers(text):
    words = {w.rstrip("s") for w in re.findall(r"[a-z-]+", text.lower()) if w in QUALIFIERS or w.rstrip("s") in QUALIFIERS}
    return words | set(re.findall(r"\$\d+", text))   # "shipping on a $90 order" is narrower than "shipping"


def strip_today(text):
    """'what time do you close today' asks the same as 'what time do you close'."""
    return re.sub(r"\s*\b(today|tonight|right now|now)\b", "", text, flags=re.I).strip() or text


PROFILE_NOTE = "A customer of the store: the purchases below come from the store system."


def alteration_title(order):
    """Which item was altered, by name: 'hem on order NY-2460' left the model guessing."""
    a = order["alteration"]
    items = [it["item"].split(",")[0] for it in order["items"] if it["item"].startswith(("Harbour", "Inis"))]
    what = f"{a['what']} on the {' / '.join(items)}" if items else a["what"]
    return f"Alteration ({what}) on order {order['order']}, ready for pickup from {a['ready']}"


MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
DAYS = "Monday Tuesday Wednesday Thursday Friday Saturday Sunday".split()
NAMED_DATE = re.compile(r"\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)([a-z]*)(,? )(" + "|".join(MONTHS) + r")([a-z]*\.? )(\d{1,2})\b")


def fix_weekdays(text, today):
    """Correct the weekday the model wrote beside a date ("Thu Jan 15" -> "Fri Jan 15"), taking
    the year that puts the date nearest today. Haiku got weekdays wrong three times with a
    calendar in front of it; a lookup never does."""
    now = datetime.date.fromisoformat(today)

    def fix(m):
        try:
            dates = [datetime.date(y, MONTHS.index(m.group(4)) + 1, int(m.group(6))) for y in (now.year - 1, now.year, now.year + 1)]
        except ValueError:
            return m.group(0)
        real = DAYS[min(dates, key=lambda d: abs((d - now).days)).weekday()]
        day = real if m.group(2) else real[:3]
        return day + m.group(0)[len(m.group(1) + m.group(2)):]
    return NAMED_DATE.sub(fix, text)


def deadline(title, today):
    """The return deadline of one purchase line, worked out here so the model only reads it.
    The model did this arithmetic itself and missed the holiday window and an online sale
    window that had plainly closed."""
    hit = re.search(r", (full price|SALE[^,]*), order \S+ (in store|online) on (\d{4}-\d{2}-\d{2})", title)
    if not hit:
        return ""
    tag, where, bought = hit.group(1), hit.group(2), datetime.date.fromisoformat(hit.group(3))
    now = datetime.date.fromisoformat(today)
    sale = tag.startswith("SALE")
    days = 14 if sale else 30
    end = bought + datetime.timedelta(days=days)
    holiday = not sale and datetime.date(bought.year, 11, 1) <= bought <= datetime.date(bought.year, 12, 24)
    if holiday:
        end = max(end, datetime.date(bought.year + 1, 1, 15))
    kind = "store credit or exchange only" if sale else "refund"
    fmt = lambda d: d.strftime("%a %b %d %Y").replace(" 0", " ")
    if where == "online" and not holiday:
        latest = end + datetime.timedelta(days=14)   # delivery is not recorded; standard shipping takes 3-5 business days
        if latest < now:
            return f" RETURN WINDOW CLOSED (online, {days} days from delivery, long past)."
        return f" RETURN: {kind}, {days} days from delivery, so at least until {fmt(end)}."
    if end < now:
        return f" RETURN WINDOW CLOSED on {fmt(end)}."
    note = " (Thanksgiving, store closed: come by the day before)" if (end.month, end.day) == (11, 26) else ""
    return f" RETURN: {kind} until {fmt(end)}{note}."


def parse_reply(text):
    """The JSON object in the model's reply. Sonnet sometimes writes the answer as prose first
    and the JSON after it in a code block, so try every '{' from the last one back and keep the
    first that parses and carries an answer."""
    starts = [i for i, ch in enumerate(text) if ch == "{"]
    for i in reversed(starts):
        try:
            out, _ = json.JSONDecoder().raw_decode(text[i:])
        except ValueError:
            continue
        if isinstance(out, dict) and out.get("answer"):
            return out
    raise ValueError("no JSON object with an answer")


def load_classifier():
    """The GPU when this PyTorch can use it, otherwise the CPU (a few milliseconds a question
    either way: the model is 90 MB)."""
    name = "sentence-transformers/all-MiniLM-L6-v2"
    if torch.cuda.is_available():
        try:
            model = SentenceTransformer(name, device="cuda")
            model.encode("test")
            return model, "cuda"
        except RuntimeError:  # e.g. a GPU newer than this PyTorch build
            pass
    return SentenceTransformer(name, device="cpu"), "cpu"


class BrainBot:
    """What the whole shop shares: the brain, the two local models and the known questions.
    Several customers chat at once, so whatever they share is changed under one lock."""

    llm_model = ANSWER_MODEL  # what the harness prices the answers at

    def __init__(self):
        self.lock = threading.RLock()
        self.model, self.device = load_classifier()
        self.reranker = CrossEncoder("BAAI/bge-reranker-base", device=self.device)
        self.reranker.predict([("warm up", "warm up")])
        self.vectors = {}
        # The shop owner's corrections go into the brain once, like any other fact.
        have = {m["title"] for c in CORRECTIONS for m in browse(c["scope"])}
        for c in CORRECTIONS:
            if c["title"] not in have:
                remember(c["scope"], "decision", c["title"], c["body"], "--source", "shop/corrections.json",
                         "--confidence", "confirmed", "--allow-duplicate")
        for c in CURATED:
            stored = {m["title"]: m for m in browse(c["scope"])}
            if c["title"] not in stored:
                remember(c["scope"], "knowledge", c["title"], c["body"], "--source", "shop/corrections.json",
                         "--confidence", "confirmed", "--allow-duplicate")
            elif stored[c["title"]]["body"] != c["body"]:
                # The owner reworded an answer: correct it in place, the old version stays in history.
                ns("update", str(stored[c["title"]]["id"]), "--body", c["body"])
        # The classifier compares a question with every scope's description and with the
        # titles filed in that scope, so it gets better as the brain grows.
        self.labels, texts = [], []
        for scope, description in SCOPES.items():
            for m in [{"title": description}] + browse(scope):
                self.labels.append(scope)
                texts.append(m["title"])
        self.scope_vecs = self.model.encode(texts, convert_to_tensor=True, normalize_embeddings=True)
        # Known questions: memories whose title is a question. Their answer is the body. Plus
        # the other wordings learned on earlier days (see learn_phrasing).
        self.known = [m for s in SCOPES for m in browse(s)
                      if m["title"].rstrip().endswith("?") and not STAFF_TEXT.search(m["body"])]
        if PHRASINGS.exists():
            for p in json.loads(PHRASINGS.read_text()):
                self.known.append({**get(p["id"]), "title": p["phrasing"]})
        self.known_vecs = self.model.encode([m["title"] for m in self.known], convert_to_tensor=True,
                                            normalize_embeddings=True)
        # The core facts, read once from the brain and sent first in every call.
        core = [m for s in CORE for m in browse(s) if not m["title"].rstrip().endswith("?")]
        self.core_ids = {m["id"] for m in core}
        self.core = "\n".join(f"- {clean_fact(m['title'])}" for m in core)   # titles are whole sentences with the numbers

    def start_chat(self, email, date):
        return Conversation(self, email, date)

    def encode(self, text):
        with self.lock:
            return self.model.encode(text, convert_to_tensor=True, normalize_embeddings=True)

    def classify(self, text):
        """The two scopes this text most likely belongs to, by the local model."""
        q = self.encode(text)
        sims = (self.scope_vecs @ q).tolist()
        best = {}
        for label, s in zip(self.labels, sims):
            best[label] = max(best.get(label, 0), s)
        return sorted(best, key=best.get, reverse=True)[:2], q

    def vector(self, memory):
        """A memory's title, embedded once and kept: the brain does not change under us
        except through this bot."""
        if memory["id"] not in self.vectors:
            self.vectors[memory["id"]] = self.encode(f"{memory['title']}. {memory['body']}")
        return self.vectors[memory["id"]]

    def learn_phrasing(self, phrasing, memory, keep=False):
        """Add a wording to the known questions, pointing at the memory that answers it. A
        wording that is not itself a memory's title is kept in phrasings.json beside the brain,
        so it survives a restart."""
        with self.lock:
            self.known.append({**memory, "title": phrasing})
            self.known_vecs = torch.cat([self.known_vecs, self.encode([phrasing])])
            if keep:
                kept = json.loads(PHRASINGS.read_text()) if PHRASINGS.exists() else []
                PHRASINGS.write_text(json.dumps(kept + [{"phrasing": phrasing, "id": memory["id"]}], indent=1))

    def known_answer(self, message, q, scopes):
        """Step 3: a known question, asked in other words, gets the stored answer. Only known
        questions filed in the scopes just picked are candidates; the nearest three by the
        small model go to the cross-encoder, which has the last word."""
        with self.lock:
            known = list(self.known)
            # The same words as a question already answered: no model needed to know that.
            plain = normalise(message)
            # The owner's answers first, against every question: a short answer the bot learned
            # ("do you ship" -> "US only") must not beat the owner's complete one. Real matches
            # score 0.82-1.00 with the cross-encoder, unrelated questions 0.00.
            curated = [m for m in known if m.get("source") == "shop/corrections.json"]
            seen_ids, owner = set(), []
            for m in curated:
                if m["id"] not in seen_ids:
                    seen_ids.add(m["id"])
                    owner.append(m)
            if owner:
                scores = self.reranker.predict([(message, m["title"]) for m in owner]).tolist()
                j = max(range(len(owner)), key=scores.__getitem__)
                if scores[j] >= OWNER_SAME and not (qualifiers(owner[j]["title"]) - qualifiers(message)):
                    return owner[j], 1.0, round(scores[j], 3)
            for m in reversed(known):
                if normalise(m["title"]) == plain:
                    return m, 1.0, 1.0
            sims = (self.known_vecs @ q).tolist()
            # The three nearest DIFFERENT memories: several wordings of one question must not
            # crowd out a closer question ("returns policy" vs "returns policy over Christmas").
            near, seen = [], set()
            for i in sorted(range(len(sims)), key=sims.__getitem__, reverse=True):
                if sims[i] < NEAR or len(near) == 3:
                    break
                # A stored answer may come from the question's scope or a scope above it, never
                # from a narrower one: "return policy" must not get the sale-items answer.
                if any(s == known[i]["scope"] or s.startswith(known[i]["scope"] + ":") for s in scopes) \
                        and not (qualifiers(known[i]["title"]) - qualifiers(message)) \
                        and known[i]["id"] not in seen:
                    near.append(i)
                    seen.add(known[i]["id"])
            if not near:
                return None
            scores = self.reranker.predict([(message, known[i]["title"]) for i in near]).tolist()
        # The owner's curated answer wins whenever it passes, then the best of the rest.
        passing = [j for j in range(len(near)) if scores[j] >= SAME]
        if not passing:
            return None
        curated = [j for j in passing if known[near[j]].get("source") == "shop/corrections.json"]
        k = max(curated or passing, key=scores.__getitem__)
        return known[near[k]], round(sims[near[k]], 3), round(scores[k], 3)


class Conversation:
    """One customer's chat. It holds what was said so far; everything else is the shop's."""

    def __init__(self, shop, email, date):
        self.shop, self.date, self.history, self.asked = shop, date, [], []
        self.weekday = datetime.date.fromisoformat(date).strftime("%A")
        self.customer = "customer:" + re.sub(r"[^a-z]+", "-", email.split("@")[0].lower()).strip("-")
        self.name = ORDERS.get(email, {}).get("name")
        self.has_orders = bool(ORDERS.get(email, {}).get("orders"))
        if browse(self.customer):
            with shop.lock:
                self.bring_records_up_to_date(email)
        elif email in ORDERS:
            # First login: copy the order history from the order system into the brain.
            c = ORDERS[email]
            with shop.lock:
                remember(self.customer, "knowledge", f"Customer {c['name']}, logs in as {email}",
                         PROFILE_NOTE, "--source", "shop/orders.json", "--confidence", "confirmed")
                for o in c["orders"]:
                    for it in o["items"]:
                        remember(self.customer, "knowledge",
                                 f"Bought {it['item']}, size {it['size']}, ${it['price']}, {it['tag']}, order {o['order']} {o['where']} on {o['date']}",
                                 f"Order {o['order']}, bought {o['where']} on {o['date']}.", "--date", o["date"],
                                 "--source", "shop/orders.json", "--confidence", "confirmed", "--allow-duplicate")
                    if "alteration" in o:
                        a = o["alteration"]
                        remember(self.customer, "knowledge", alteration_title(o),
                                 "At the alterations counter, lower floor, during store hours.", "--date", o["date"],
                                 "--source", "shop/orders.json", "--confidence", "confirmed", "--allow-duplicate")

    def lookup(self, message):
        """Answers that are plain look-ups in this customer's records in the brain."""
        m = message.lower()
        if not self.name or not re.search(r"points do i have|is my hem|hem ready|what size .* did i buy|which size did i", m):
            return None
        records = browse(self.customer)
        bought = [r for r in records if r["title"].startswith("Bought ")]
        if "points" in m:
            spent = sum(int(x) for r in bought for x in re.findall(r"\$(\d+)", r["title"])[:1])
            return (f"You have about {spent} Row Rewards points: 1 point for every $1 you spent with us. Every 200 "
                    f"points is a $10 reward, and points expire 12 months after you earn them. Returned items take "
                    f"their points off.")
        if "hem" in m:
            alt = [r for r in records if r["title"].startswith("Alteration")]
            if not alt:
                return ("I don't see an alteration on your account. If you left something with us, the alterations "
                        "counter (lower floor) or (212) 555-0147 can check it.")
            return f"{alt[0]['title']}. {alt[0]['body']}"
        words = [w for w in re.findall(r"[a-z]+", m) if len(w) > 3 and w not in {"what", "size", "which", "did", "buy"}]
        hits = [r for r in bought if any(w in r["title"].lower() for w in words)] or bought
        return " ".join(r["title"].replace("Bought", "You bought", 1) + "." for r in hits[:2])

    def bring_records_up_to_date(self, email):
        """Customers who logged in before a correction keep the old wording in the brain, so it is
        corrected the way any record is: ns-brain update, in place."""
        orders = {o["order"]: o for o in ORDERS.get(email, {}).get("orders", [])}
        for m in browse(self.customer):
            if m["body"].startswith("A Row Rewards member"):
                ns("update", str(m["id"]), "--body", PROFILE_NOTE)
            hit = re.match(r"Alteration \(\w+\) on order (\S+), ready", m["title"])
            if hit and hit.group(1) in orders:
                ns("update", str(m["id"]), "--title", alteration_title(orders[hit.group(1)]))

    def profile(self):
        # Every date carries its weekday: the model once called Monday Nov 9 a Sunday.
        def weekday(match):
            return datetime.date.fromisoformat(match.group(0)).strftime("%a %Y-%m-%d")
        records = browse(self.customer)
        # An altered item is marked on its own line: the model once had the hem record and the
        # "altered items can't be returned" rule and still offered a return.
        altered = {}
        for r in records:
            hit = re.match(r"Alteration \((\w+) on the (.+?)\) on order (\S+),", r["title"])
            if hit:
                for item in hit.group(2).split(" / "):
                    altered[(hit.group(3), item)] = hit.group(1)
        lines = []
        for r in records:
            line = f"- {r['title']}. {r['body']}"
            marked = False
            for (order, item), what in altered.items():
                if r["title"].startswith(f"Bought {item}") and f"order {order}" in r["title"]:
                    line += f" ALTERED ({what}): can't be returned or exchanged."
                    marked = True
            if r["title"].startswith("Bought ") and not marked:
                line += deadline(r["title"], self.date)
            lines.append(line)
        return re.sub(r"\b\d{4}-\d{2}-\d{2}\b", weekday, "\n".join(lines))

    def needs_context(self, message):
        # A short message needs the chat before it only when there is one: "store hours?"
        # opening a chat is a whole question, "and sunday?" after it is not.
        return bool(OWN.search(message) or FOLLOW_UP.search(message)
                    or (self.history and PRONOUN.search(message)))

    def reply(self, message):
        t0 = time.perf_counter()
        context = " ".join(self.asked[-1:] + [message])
        self.asked.append(message)
        shop = self.shop
        asked = expand(strip_today(message))
        if not self.needs_context(message) or re.search(r"\b(hours?|open|close|late|sunday|saturday|thursday)\b", message, re.I):
            after_hours = len(self.asked) > 1 and bool(hours_answer(self.asked[-2], self.date))
            hours = hours_answer(message, self.date, after_hours)
            if hours and not OWN.search(message):
                self.history += [{"role": "user", "content": message}, {"role": "assistant", "content": hours}]
                return hours, {"usage": {}, "llm_calls": 0, "path": "memory", "scopes": ["hours"],
                               "matched": "store hours", "local_ms": round((time.perf_counter() - t0) * 1000, 1)}
        scopes, q = shop.classify(context if FOLLOW_UP.search(message) else asked)

        # Step 2b: questions about this customer's own records are answered from the brain
        # by code, with no model: points, an alteration, a size bought.
        looked_up = self.lookup(message)
        if looked_up:
            self.history += [{"role": "user", "content": message}, {"role": "assistant", "content": looked_up}]
            return looked_up, {"usage": {}, "llm_calls": 0, "path": "memory", "scopes": [self.customer],
                               "matched": "customer record", "local_ms": round((time.perf_counter() - t0) * 1000, 1)}

        # Step 3: answer from memory when this is a question the shop already answered.
        # A logged-in customer with purchases who asks about returns or exchanges gets an answer
        # built from those purchases, never the general policy: "can i return something" from
        # Rosa means her boots and her hemmed trousers.
        own_returns = self.has_orders and any(s.split(":")[0] in ("returns", "exchanges") for s in scopes)
        found = None if self.needs_context(message) or own_returns else shop.known_answer(asked, q, scopes)
        if found:
            hit, similarity, same = found
            answer = clean_fact(hit["body"])
            self.history += [{"role": "user", "content": message}, {"role": "assistant", "content": answer}]
            return answer, {"usage": {}, "llm_calls": 0, "path": "memory", "scopes": scopes,
                            "matched": hit["title"], "similarity": similarity, "same": same,
                            "local_ms": round((time.perf_counter() - t0) * 1000, 1)}
        local_ms = (time.perf_counter() - t0) * 1000

        # Step 4: the LLM, with only the facts that matter. The candidates are everything
        # filed in the two scopes plus ns-brain's best word matches from the scopes above them
        # (recall in delivery:eu also searches delivery and global). The local model then
        # keeps the FACTS closest in meaning to the question, so a question about France
        # finds "EU delivery" although the two share no word.
        candidates = {}
        for s in scopes:
            for m in browse(s, n=60) + recall(message, s, n=8):
                if not m["scope"].startswith("customer"):
                    candidates[m["id"]] = m
        candidates = {k: m for k, m in candidates.items() if k not in shop.core_ids}
        ranked = sorted(candidates.values(), key=lambda m: float(shop.vector(m) @ q), reverse=True)[:FACTS]
        lines = [f"- [{m['scope']}] {clean_fact(m['title'])}. {clean_fact(m['body'])}" for m in ranked]
        # The customer's records only for questions that can use them: on "how long does it
        # take" after "do you ship", a hem waiting for pickup pulled the model off topic.
        relevant = OWN.search(message) or any(s.split(":")[0] in ("returns", "exchanges", "alterations", "rewards")
                                              for s in scopes)
        profile = self.profile() if relevant else ""
        previous = f"\nTHE CUSTOMER'S PREVIOUS MESSAGE: {self.asked[-2]}" if len(self.asked) > 1 else ""
        # Weekdays come from a calendar, never from the model's own arithmetic: it once called
        # Saturday Nov 21 a Friday.
        start = datetime.date.fromisoformat(self.date)
        calendar = ", ".join((start + datetime.timedelta(days=i)).strftime("%a %b %d") for i in range(0, 60))
        prompt = (f"TODAY: {self.date} ({self.weekday})\nCALENDAR (use it for every weekday you name): {calendar}"
                  f"\n\nWHAT THE SHOP KNOWS ABOUT THIS CUSTOMER:\n{profile or '- not needed for this question'}"
                  f"\n\nSHOP FACTS FOR THIS QUESTION:\n" + "\n".join(lines) + f"{previous}\n\nCUSTOMER: {message}")
        # The rules, the format and the core facts are the same in every call, so they come
        # first and are cached: from the second call on they cost a tenth of the input price.
        msgs = [{"role": "system", "cache_control": {"type": "ephemeral"},
                 "content": f"{RULES}\n{ANSWERING}\n\n"
                 f"{FORMAT}\n\nCORE STORE FACTS:\n{shop.core}"}]
        msgs += [{"role": m["role"], "content": m["content"]} for m in self.history]
        msgs.append({"role": "user", "content": prompt})
        # Room for the model's reasoning and a long answer: a customer with many purchases once
        # got an empty reply because both did not fit in 1,500 tokens. Only what is used is billed.
        text, usage = chat(msgs, max_tokens=4000, model=ANSWER_MODEL)
        if not text.strip():
            # The model spent every token reasoning and wrote nothing (it happened once, on a
            # customer with many purchases). Ask once more, briefly, and pay for both calls.
            retry = msgs[:-1] + [{"role": "user", "content": prompt + "\n\nReason briefly and answer now."}]
            text, more = chat(retry, max_tokens=4000, model=ANSWER_MODEL)
            usage = {k: usage.get(k, 0) + more.get(k, 0) for k in set(usage) | set(more)}
        if not text.strip():
            owner = shop.known_answer(asked, q, scopes)
            text = json.dumps({"answer": clean_fact(owner[0]["body"]) if owner else
                               "Sorry, I couldn't answer that just now. The store team can help at "
                               "ny@kestrelrow.example or (212) 555-0147."})
        try:
            out = parse_reply(text)
        except ValueError:
            # Broken or cut-off JSON: take the text of the "answer" field if it is there, so the
            # customer never sees the program's format; otherwise the prose before any code block.
            m = re.search(r'"answer"\s*:\s*"((?:[^"\\]|\\.)*)', text)
            if m:
                out = {"answer": m.group(1).replace('\\"', '"').replace("\\n", "\n").strip()}
            else:
                out = {"answer": text.split("```")[0].strip() or text}
        answer = fix_weekdays(out.get("answer") or text, self.date)

        # Step 5: teach the brain.
        learned, refused = [], []
        general, general_answer = out.get("general_question"), answer
        name = self.name.split()[0].lower() if self.name else None
        # Only a guest's chat teaches the store an answer for everyone. A logged-in customer's
        # answer was written with their purchases in front of the model, and once it carried
        # one customer's order to another; no word list is a safe enough filter for that.
        if general and (self.name or ORDER_NUMBER.search(answer) or PERSONAL_ANSWER.search(answer)
                        or STAFF_TEXT.search(answer) or (name and name in answer.lower())):
            refused.append(f"{general} (not stored: a customer's chat, or about a person)")
            general = None
        if general and not OWN.search(general) and not self.needs_context(message):
            title = general.rstrip(" ?") + "?"
            with shop.lock:
                mid, existing = remember(scopes[0], "knowledge", title, general_answer,
                                         "--source", "bot", "--confidence", "reported")
            if mid:
                memory = {"id": mid, "title": title, "body": general_answer, "scope": scopes[0]}
                shop.learn_phrasing(title, memory)
                # and the customer's own words, so the next customer who types them gets it at once
                if normalise(asked) != normalise(title):
                    shop.learn_phrasing(asked, memory, keep=True)
                learned.append(title)
            elif existing:
                # ns-brain refused a second answer to a question it already holds and named
                # the memory that has it. Nothing new goes in the brain; the bot learns that
                # this customer's words are another way to ask that question.
                shop.learn_phrasing(asked, get(existing), keep=True)
                refused.append(f"{title} (already #{existing})")
        for note in out.get("customer_notes") or []:
            with shop.lock:
                mid, existing = remember(self.customer, "preference", note, f"Said in chat on {self.date}.",
                                         "--source", "bot", "--confidence", "reported", "--date", self.date)
            (learned if mid else refused).append(note)

        # The history keeps only what the customer and the shop said, not the facts sent with
        # each message: the next message brings its own facts.
        self.history += [{"role": "user", "content": message}, {"role": "assistant", "content": answer}]
        return answer, {"usage": usage, "llm_calls": 1, "path": "brain+llm", "scopes": scopes,
                        "facts": len(lines), "learned": learned, "refused": refused, "local_ms": round(local_ms, 1)}


if __name__ == "__main__":
    run(BrainBot(), os.environ.get("LEVEL_NAME", "level3"))
