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
from brain import RUN, SCOPES, browse, get, recall, remember

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

# How many facts go to the LLM with a question, ranked by the local model. Fourteen lost
# facts the answers needed (Harbour runs small; exchanges are Ireland and UK only).
FACTS = 22

# Messages about this customer's own orders always go to the LLM, and so do messages that
# need the previous one to make sense: a stored answer cannot know which jumper "it" is. A
# pronoun only counts when there IS a previous message ("how much does it cost?" opening a
# chat is a general question).
OWN = re.compile(r"\b(i bought|i got|i ordered|did i buy|i posted|last time|for me|recommend|"
                 r"receipt for the|do i have to return the|points do i have|is my|"
                 r"my (order|parcel|items?|stuff|size|points|account|hem|jeans|dress|shirts?|boots|jacket|return))\b"
                 r"|\bthe [a-z ]+ (is|are) too\b", re.I)
FOLLOW_UP = re.compile(r"^\s*(and|also|but|what about|how about)\b", re.I)
PRONOUN = re.compile(r"\b(it|them|that one|this one|these|those)\b", re.I)

FORMAT = """Reply with one JSON object and nothing else:
{"answer": "<your reply to this customer>",
 "general_question": "<only if your answer contains nothing about this customer, does not depend on today's date or day of the week, and would be word for word right for anyone (a store policy, hours, payment, care...): the question rewritten as a short self-contained question; otherwise null>",
 "customer_notes": ["<each new durable fact about THIS customer worth remembering next time: sizes, fit, likes, dislikes, what they plan to buy, and any size or product you recommended to them; one short sentence each, or none>"]}"""


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
        self.known = [m for s in SCOPES for m in browse(s) if m["title"].rstrip().endswith("?")]
        if PHRASINGS.exists():
            for p in json.loads(PHRASINGS.read_text()):
                self.known.append({**get(p["id"]), "title": p["phrasing"]})
        self.known_vecs = self.model.encode([m["title"] for m in self.known], convert_to_tensor=True,
                                            normalize_embeddings=True)

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
            known, sims = list(self.known), (self.known_vecs @ q).tolist()
            # The three nearest DIFFERENT memories: several wordings of one question must not
            # crowd out a closer question ("returns policy" vs "returns policy over Christmas").
            near, seen = [], set()
            for i in sorted(range(len(sims)), key=sims.__getitem__, reverse=True):
                if sims[i] < NEAR or len(near) == 3:
                    break
                if known[i]["scope"] in scopes and known[i]["id"] not in seen:
                    near.append(i)
                    seen.add(known[i]["id"])
            if not near:
                return None
            scores = self.reranker.predict([(message, known[i]["title"]) for i in near]).tolist()
        k = max(range(len(near)), key=scores.__getitem__)
        if scores[k] < SAME:
            return None
        return known[near[k]], round(sims[near[k]], 3), round(scores[k], 3)


class Conversation:
    """One customer's chat. It holds what was said so far; everything else is the shop's."""

    def __init__(self, shop, email, date):
        self.shop, self.date, self.history, self.asked = shop, date, [], []
        self.customer = "customer:" + re.sub(r"[^a-z]+", "-", email.split("@")[0].lower()).strip("-")
        if not browse(self.customer) and email in ORDERS:
            # First login: copy the order history from the order system into the brain.
            c = ORDERS[email]
            with shop.lock:
                remember(self.customer, "knowledge", f"Customer {c['name']}, logs in as {email}",
                         "A Row Rewards member: purchases below come from the store system.",
                         "--source", "shop/orders.json", "--confidence", "confirmed")
                for o in c["orders"]:
                    for it in o["items"]:
                        remember(self.customer, "knowledge",
                                 f"Bought {it['item']}, size {it['size']}, ${it['price']}, {it['tag']}, order {o['order']} {o['where']} on {o['date']}",
                                 f"Order {o['order']}, bought {o['where']} on {o['date']}.", "--date", o["date"],
                                 "--source", "shop/orders.json", "--confidence", "confirmed", "--allow-duplicate")
                    if "alteration" in o:
                        a = o["alteration"]
                        remember(self.customer, "knowledge",
                                 f"Alteration ({a['what']}) on order {o['order']}, ready for pickup from {a['ready']}",
                                 "At the alterations counter, lower floor, during store hours.", "--date", o["date"],
                                 "--source", "shop/orders.json", "--confidence", "confirmed", "--allow-duplicate")

    def profile(self):
        return "\n".join(f"- {m['title']}. {m['body']}" for m in browse(self.customer))

    def needs_context(self, message):
        # A short message needs the chat before it only when there is one: "store hours?"
        # opening a chat is a whole question, "and sunday?" after it is not.
        return bool(OWN.search(message) or FOLLOW_UP.search(message)
                    or (self.history and (len(message.split()) < 4 or PRONOUN.search(message))))

    def reply(self, message):
        t0 = time.perf_counter()
        context = " ".join(self.asked[-1:] + [message])
        self.asked.append(message)
        shop = self.shop
        scopes, q = shop.classify(context if FOLLOW_UP.search(message) else message)

        # Step 3: answer from memory when this is a question the shop already answered.
        found = None if self.needs_context(message) else shop.known_answer(message, q, scopes)
        if found:
            hit, similarity, same = found
            answer = hit["body"]
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
        ranked = sorted(candidates.values(), key=lambda m: float(shop.vector(m) @ q), reverse=True)[:FACTS]
        lines = [f"- [{m['scope']}] {m['title']}. {m['body']}" for m in ranked]
        prompt = (f"TODAY: {self.date}\n\nWHAT THE SHOP KNOWS ABOUT THIS CUSTOMER:\n{self.profile() or '- nothing yet'}"
                  f"\n\nSHOP FACTS FOR THIS QUESTION:\n" + "\n".join(lines) + f"\n\nCUSTOMER: {message}")
        # No prompt caching here: the whole request is about 2,000 tokens and changes with
        # every question, and Anthropic does not cache prefixes under 1,024 tokens anyway.
        msgs = [{"role": "system", "content": f"{RULES}\nUse the shop facts and what the shop knows about the "
                 f"customer; for returns, list the customer's items and say for each whether and until when it can "
                 f"be returned.\n\n{FORMAT}"}]
        msgs += [{"role": m["role"], "content": m["content"]} for m in self.history]
        msgs.append({"role": "user", "content": prompt})
        text, usage = chat(msgs, model=ANSWER_MODEL)
        try:
            out = parse_reply(text)
        except ValueError:
            # No usable JSON at all: show the prose, never a code block meant for the program.
            out = {"answer": text.split("```")[0].strip() or text}
        answer = out.get("answer") or text

        # Step 5: teach the brain.
        learned, refused = [], []
        general, general_answer = out.get("general_question"), answer
        if general and not OWN.search(general):
            title = general.rstrip(" ?") + "?"
            with shop.lock:
                mid, existing = remember(scopes[0], "knowledge", title, general_answer,
                                         "--source", "bot", "--confidence", "reported")
            if mid:
                shop.learn_phrasing(title, {"id": mid, "title": title, "body": general_answer, "scope": scopes[0]})
                learned.append(title)
            elif existing:
                # ns-brain refused a second answer to a question it already holds and named
                # the memory that has it. Nothing new goes in the brain; the bot learns that
                # this customer's words are another way to ask that question.
                shop.learn_phrasing(message, get(existing), keep=True)
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
    run(BrainBot(), "level3")
