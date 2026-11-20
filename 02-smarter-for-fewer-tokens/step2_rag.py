"""Step 2: retrieval (RAG) and prompt caching, on the same strong model.

# The idea

Step 1 pays for the whole handbook on every message although each question needs a page of it
at most. Retrieval sends only that page. The handbook is cut once into passages and each passage
is turned into a vector: a list of numbers that describes what the passage means, so passages
about similar things get similar vectors. For every message the question is turned into a
vector the same way and the five passages closest to it go to the model. This is what is called
retrieval-augmented generation, RAG.

# Prompt caching

The rules and the chat so far are the same from one message of a chat to the next. Marked as
cacheable, they are read back from the provider's cache on the next message at a tenth of the
input price. The retrieved passages sit inside each user message, so they become part of that
cached history too.

# What it still cannot do

It knows the handbook and nothing about the person asking. "Can I return the jacket I bought?"
gets the general returns policy and "it depends", because the bot cannot see what was bought.

    python3 step2_rag.py 1          (builds index.json first if it is missing)
"""
import copy
import json
import math
import pathlib

from harness import EMBED_PRICE, RULES, SHOP, SONNET, add, chat, embed, run

HERE = pathlib.Path(__file__).resolve().parent
INDEX_FILE = HERE / "index.json"
TOP_K = 5                       # passages sent with each question
MAX_WORDS = 350                 # a longer subsection is split so one passage stays about one topic
CACHE = {"type": "ephemeral"}   # Anthropic's marker: "everything up to here may be cached"


def passages(markdown):
    """Cut the handbook into passages: one per "###" subsection, split when it is long. Each piece
    keeps its headings, so "Express: 2 days" still says which shipping zone it is about."""
    section = sub = ""
    chunks, lines = [], []

    def flush():
        words = "\n".join(lines).split()
        for i in range(0, len(words), MAX_WORDS):
            chunks.append({"heading": f"{section} > {sub}".strip(" >"), "text": " ".join(words[i:i + MAX_WORDS])})

    for line in markdown.splitlines():
        if line.startswith(("## ", "### ")):
            flush()
            lines = []
            if line.startswith("## "):
                section, sub = line[3:].strip(), ""
            else:
                sub = line[4:].strip()
        else:
            lines.append(line)
    flush()
    return chunks


def build_index():
    """Embed every passage once and keep the vectors in index.json. This is a one-off cost,
    reported apart from the chats (a fraction of a cent)."""
    chunks = passages((SHOP / "handbook.md").read_text())
    tokens = 0
    for i in range(0, len(chunks), 64):
        batch = chunks[i:i + 64]
        vectors, t = embed([f"{c['heading']}\n{c['text']}" for c in batch])
        tokens += t
        for c, v in zip(batch, vectors):
            c["vector"] = v
    INDEX_FILE.write_text(json.dumps({"tokens": tokens, "cost": tokens * EMBED_PRICE / 1e6, "passages": chunks}))
    print(f"index: {len(chunks)} passages, {tokens} tokens, ${tokens * EMBED_PRICE / 1e6:.5f}")


def cosine(a, b):
    """How close two vectors point, from -1 to 1: the usual measure of 'similar in meaning'."""
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


class RagBot:
    model = SONNET

    def __init__(self):
        if not INDEX_FILE.exists():
            build_index()
        self.index = json.loads(INDEX_FILE.read_text())["passages"]

    def start_chat(self, email, date):
        # Like step 1, no memory of customers: the login is ignored.
        conversation = copy.copy(self)
        conversation.system = {"role": "system", "cache_control": CACHE,
                               "content": f"{RULES}\nToday is {date}.\nEach customer message comes with "
                                          "extracts from the store's handbook. Answer from those extracts only."}
        conversation.history = []
        return conversation

    def query_vector(self, message):
        """The vector the passages are searched with. "and on sunday?" means nothing on its own,
        so the search also uses the question before it."""
        previous = [m["question"] for m in self.history if m["role"] == "user"][-1:]
        [q], embed_tokens = embed([" ".join(previous + [message])])
        return q, embed_tokens

    def reply(self, message):
        q, embed_tokens = self.query_vector(message)
        return self.answer(message, q, embed_tokens)

    def answer(self, message, q, embed_tokens):
        """Retrieve the five closest passages and ask the model."""
        top = sorted(self.index, key=lambda p: cosine(q, p["vector"]), reverse=True)[:TOP_K]
        extracts = "\n\n".join(f"[{p['heading']}]\n{p['text']}" for p in top)
        self.history.append({"role": "user", "question": message,
                             "content": f"HANDBOOK EXTRACTS:\n{extracts}\n\nCUSTOMER: {message}"})
        # Only role and content go to the API. The cache marker goes on the newest message, so the
        # next call can read everything up to and including it from the cache.
        messages = [self.system] + [{"role": m["role"], "content": m["content"]} for m in self.history]
        messages[-1] = {**messages[-1], "cache_control": CACHE}
        answer, usage = chat(messages, self.model)
        self.history.append({"role": "assistant", "content": answer})
        return answer, {"usage": add(usage, {"embed": embed_tokens}), "path": "rag+llm",
                        "passages": [p["heading"] for p in top]}


if __name__ == "__main__":
    run(RagBot(), "step2-rag-sonnet")
