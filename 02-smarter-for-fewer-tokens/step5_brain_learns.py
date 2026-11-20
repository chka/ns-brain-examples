"""Step 5: the brain learns. Questions the store has already answered are answered from memory,
with no model call: faster and cheaper with every day the store is open.

# The observation

A store hears the same dozen questions all day: hours, returns, Apple Pay, parking. Step 4 pays
the model to write the answer to "what are your hours" fresh every single time. Once the store
has a good answer to a question, the next customer who asks it can simply get that answer.

# Known questions

A known question is a memory whose title is a question and whose body is its answer. The brain
starts with the handbook's FAQ (built by brain.py) and grows as the bot answers new questions:

1. Teach. With every answer the model now also says whether that answer would be right for
   anyone (a policy, hours, payment) and, if so, writes the question as a short self-contained
   question. The bot stores the pair as a known question, marked confidence "reported" (written
   by the bot, not checked by a person) so the store can review what its bot learned. ns-brain
   refuses a second memory about a subject it already holds and names the one that has it. When
   that memory is a known question and the cross-encoder agrees it is the same question, the bot
   keeps the customer's wording as another way of asking it (phrasings.json, beside the brain),
   so the next customer who types those words gets the stored answer. Otherwise the new pair is
   stored beside it: ns-brain's guard compares words in titles, and "what payment methods do you
   accept?" shares enough of them with "do you accept Apple Pay?" to be refused, though one
   answer does not answer the other. Taking the refusal at its word once sent "do you take amex"
   to the Apple Pay answer and "can I return sale items" to a fact about faulty FINAL SALE items.

2. Answer from memory. Before calling the model the bot looks for a known question that means
   the same as the message. Two local models must agree. The small embedding model from step 4
   finds the nearest known questions (similarity of at least NEAR) and a cross-encoder
   (bge-reranker-base), which reads the two questions side by side, must score the pair at least
   SAME. Either model alone made mistakes the pair does not: on its own the embedding model
   matched "return policy on jumpers" with "can I return my sale jumper".

# What keeps one customer's answer away from another

Only a guest's chat (no login) teaches an answer for everyone. A logged-in customer's answer was
written with their purchases in front of the model and no word filter is a safe enough guard
against one customer's order reaching another. A stored answer is never served for a message
about the customer's own purchases or for one that needs the chat before it ("and sunday?"),
and a stored question that is narrower than the message ("return policy on sale items" against
"return policy") never answers it.

    python3 step5_brain_learns.py 1
"""
import json
import re

import torch
from sentence_transformers import CrossEncoder

from harness import SONNET, run
import step4_brain as step4
from step4_brain import FOLLOW_UP, OWN

# When is a message "the same question" as a known one? The thresholds of the two local models.
NEAR, SAME = 0.70, 0.85

# A message that refers back to something said earlier in the chat.
PRONOUN = re.compile(r"\b(it|them|that one|this one|these|those)\b", re.I)

# Text that talks about the person or this conversation is never stored for everyone.
PERSONAL_ANSWER = re.compile(r"\b(today|tonight|tomorrow|this (monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
                             r"you asked|since you|you mentioned|you said|you told|as we discussed|"
                             r"your (order|purchase|receipt|account|points|hem|size|jacket|jeans|shirt|dress|boots))\b", re.I)
ORDER_NUMBER = re.compile(r"\bNY-\d+\b")

# Handbook lines written for staff ("if a customer asks...") may inform the model but are never
# sent to a customer as they are.
STAFF_TEXT = re.compile(r"\b(if a customer|the customer|customers who|tell the customer|let the customer|"
                        r"explain (the|that|to)|the assistant|staff should|refer (them|the)|direct (them|the))\b", re.I)

# Words that narrow a question. A stored question carrying one the message does not use answers
# a narrower question than the one asked.
QUALIFIERS = {"sale", "online", "holiday", "holidays", "christmas", "thanksgiving", "black", "friday",
              "sunday", "saturday", "thursday", "receipt", "gift", "student", "military", "faulty", "final",
              "altered", "alteration", "tax", "express", "same-day", "kids", "canada", "california", "klarna",
              "weekend", "venmo", "amex", "apple", "google", "cash", "merino", "linen", "denim", "leather",
              "parking", "subway"}

FORMAT = """Reply with one JSON object and nothing else, short:
{"answer": "<your reply, at most 70 words>",
 "general_question": "<if the answer is the same for anyone (a store policy, hours, payment, care...), the question as a short self-contained question; else null>",
 "customer_notes": [<only facts this customer just told you about themselves, or a size you recommended; usually empty>]}
For hours give the regular week and the holiday hours, never only today's, so the answer stays true tomorrow."""


def normalise(text):
    return re.sub(r"[^a-z0-9 ]+", "", text.lower()).strip()


def qualifiers(text):
    words = {w.rstrip("s") for w in re.findall(r"[a-z-]+", text.lower()) if w in QUALIFIERS or w.rstrip("s") in QUALIFIERS}
    return words | set(re.findall(r"\$\d+", text))      # "shipping on a $90 order" is narrower than "shipping"


def parse_reply(text):
    """The JSON object in the model's reply. A model sometimes writes prose first and the JSON
    after it, so every "{" is tried from the last one back; failing that, the text itself is the
    answer and nothing is learned from it."""
    for i in reversed([i for i, ch in enumerate(text) if ch == "{"]):
        try:
            out, _ = json.JSONDecoder().raw_decode(text[i:])
        except ValueError:
            continue
        if isinstance(out, dict) and out.get("answer"):
            return out
    found = re.search(r'"answer"\s*:\s*"((?:[^"\\]|\\.)*)', text)
    return {"answer": json.loads(f'"{found.group(1)}"') if found else text.split("```")[0].strip() or text}


class LearningBot(step4.BrainBot):
    name, model = "step5-brain-learns-sonnet", SONNET

    def __init__(self):
        super().__init__()
        self.reranker = CrossEncoder("BAAI/bge-reranker-base", device="cpu")
        self.phrasings_file = self.brain.folder / "phrasings.json"
        # Known questions: memories whose title is a question, plus the wordings learned earlier.
        self.known = [m for s in step4.SCOPES for m in self.brain.browse(s)
                      if m["title"].rstrip().endswith("?") and not STAFF_TEXT.search(m["body"])]
        if self.phrasings_file.exists():
            for p in json.loads(self.phrasings_file.read_text()):
                self.known.append({**self.brain.get(p["id"]), "title": p["phrasing"]})
        self.known_vectors = self.encode([m["title"] for m in self.known])

    def start_chat(self, email, date):
        return LearningConversation(self, email, date)

    def learn_phrasing(self, phrasing, memory, keep):
        """A wording that points at a stored answer. One that is not itself a memory's title is
        written to phrasings.json, so it outlives this process."""
        self.known.append({**memory, "title": phrasing})
        self.known_vectors = torch.cat([self.known_vectors, self.encode([phrasing])])
        if keep:
            kept = json.loads(self.phrasings_file.read_text()) if self.phrasings_file.exists() else []
            self.phrasings_file.write_text(json.dumps(kept + [{"phrasing": phrasing, "id": memory["id"]}], indent=1))

    def same_question(self, question, memory):
        """Whether a stored memory is a known question that asks the same as this one."""
        return memory["title"].rstrip().endswith("?") and \
            self.reranker.predict([(question, memory["title"])]).tolist()[0] >= SAME

    def known_answer(self, question, q, scopes):
        """The known question that means the same as this one, or None (see the module comment)."""
        for m in reversed(self.known):
            if normalise(m["title"]) == normalise(question):
                return m
        similarities = (self.known_vectors @ q).tolist()
        near, seen = [], set()
        for i in sorted(range(len(similarities)), key=similarities.__getitem__, reverse=True):
            if similarities[i] < NEAR or len(near) == 3:
                break
            m = self.known[i]
            # The three nearest DIFFERENT memories, each filed in the question's scope or one above
            # it ("return policy" must not get the answer filed under returns:sale), and none
            # narrower than the question.
            if m["id"] not in seen and any(s == m["scope"] or s.startswith(m["scope"] + ":") for s in scopes) \
                    and not (qualifiers(m["title"]) - qualifiers(question)):
                near.append(m)
                seen.add(m["id"])
        if not near:
            return None
        scores = self.reranker.predict([(question, m["title"]) for m in near]).tolist()
        best = max(range(len(near)), key=scores.__getitem__)
        return near[best] if scores[best] >= SAME else None


class LearningConversation(step4.Conversation):

    def needs_context(self, message):
        """A message that cannot be answered without this chat or this customer: their purchases,
        a follow-up, or a pronoun pointing back ("is it free?" after "do you hem jeans")."""
        return bool(OWN.search(message) or FOLLOW_UP.search(message) or (self.history and PRONOUN.search(message)))

    def from_memory(self, question, scopes, q):
        if self.needs_context(question):
            return None
        hit = self.shop.known_answer(question, q, scopes)
        return {"answer": self.polish(hit["body"]), "matched": hit["title"]} if hit else None

    def system(self):
        return f"{super().system()}\n\n{FORMAT}"

    def polish(self, answer):
        """A last pass over an answer before the customer sees it. Nothing at this step."""
        return answer

    def finish(self, message, text, scopes):
        out = parse_reply(text)
        answer = self.polish(out.get("answer") or text)
        return answer, self.teach(message, answer, out, scopes)

    def teach(self, message, answer, out, scopes):
        """Store what this answer taught the store (see the module comment)."""
        shop, learned, refused = self.shop, [], []
        general = out.get("general_question")
        first_name = self.name.split()[0].lower() if self.name else None
        if general and (self.name or ORDER_NUMBER.search(answer) or PERSONAL_ANSWER.search(answer)
                        or STAFF_TEXT.search(answer) or (first_name and first_name in answer.lower())):
            refused.append(f"{general} (not stored: a customer's chat, or about a person)")
            general = None
        if general and not OWN.search(general) and not self.needs_context(message):
            title = general.rstrip(" ?") + "?"
            new_id, existing = shop.brain.remember(scopes[0], "knowledge", title, answer,
                                                   "--source", "bot", "--confidence", "reported")
            question = self.question(message)
            if new_id:
                memory = {"id": new_id, "title": title, "body": answer, "scope": scopes[0]}
                shop.learn_phrasing(title, memory, keep=False)
                if normalise(question) != normalise(title):
                    shop.learn_phrasing(question, memory, keep=True)
                learned.append(title)
            elif existing:
                held = shop.brain.get(existing)
                if shop.same_question(title, held):
                    shop.learn_phrasing(question, held, keep=True)
                    refused.append(f"{title} (already #{existing})")
                else:
                    new_id, _ = shop.brain.remember(scopes[0], "knowledge", title, answer, "--source", "bot",
                                                    "--confidence", "reported", "--allow-duplicate")
                    if new_id:
                        memory = {"id": new_id, "title": title, "body": answer, "scope": scopes[0]}
                        shop.learn_phrasing(title, memory, keep=False)
                        if normalise(question) != normalise(title):
                            shop.learn_phrasing(question, memory, keep=True)
                        learned.append(f"{title} (beside #{existing}, a different question)")
        for note in out.get("customer_notes") or []:
            new_id, _ = shop.brain.remember(self.customer, "preference", note, f"Said in chat on {self.date}.",
                                            "--source", "bot", "--confidence", "reported", "--date", self.date)
            (learned if new_id else refused).append(note)
        return {"learned": learned, "refused": refused}


if __name__ == "__main__":
    bot = LearningBot()
    run(bot, bot.name)
