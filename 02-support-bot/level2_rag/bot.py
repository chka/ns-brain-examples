"""Level 2, the good bot: retrieval (RAG) plus prompt caching.

Instead of the whole handbook, each message gets the five passages whose meaning is
closest to the question. Two changes against Level 1:

1. Retrieval. The question is embedded (one cheap call) and compared with every passage
   vector from build_index.py; only the top five go to the model.
2. Prompt caching. The rules and the chat so far are the same from one message to the
   next, so they are marked cacheable and the next message reads them at a tenth of the
   price. The retrieved passages sit inside each user message, so they become part of
   that cached history too.
"""
import json
import math
import copy
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from common import RULES, add, chat, embed, run

HERE = pathlib.Path(__file__).resolve().parent
INDEX = json.loads((HERE / "index.json").read_text())["passages"]
TOP_K = 5
CACHE = {"type": "ephemeral"}


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


class RagBot:
    def start_chat(self, email, date):
        # Like Level 1, no memory of customers: every chat starts from nothing.
        system = {"role": "system", "content": f"{RULES}\nToday is {date}.\n"
                       "Each customer message comes with extracts from the shop's handbook. "
                       "Answer from those extracts only.", "cache_control": CACHE}
        conversation = copy.copy(self)
        conversation.system = system
        conversation.history = []
        return conversation

    def reply(self, message):
        # A follow-up like "and is it free?" means nothing alone, so the search uses the
        # previous question too.
        previous = [m["question"] for m in self.history if m["role"] == "user"][-1:]
        [q], embed_tokens = embed([" ".join(previous + [message])])
        top = sorted(INDEX, key=lambda p: cosine(q, p["vector"]), reverse=True)[:TOP_K]
        extracts = "\n\n".join(f"[{p['heading']}]\n{p['text']}" for p in top)

        user = {"role": "user", "question": message,
                "content": f"HANDBOOK EXTRACTS:\n{extracts}\n\nCUSTOMER: {message}"}
        self.history.append(user)
        # Only role and content go to the API; the cache marker sits on the newest message,
        # so the next call can read everything up to here from the cache.
        msgs = [self.system] + [{"role": m["role"], "content": m["content"]} for m in self.history]
        msgs[-1]["cache_control"] = CACHE
        answer, usage = chat(msgs)
        self.history.append({"role": "assistant", "content": answer})
        return answer, {"usage": add(usage, {"embed": embed_tokens}), "llm_calls": 1, "path": "rag+llm",
                        "passages": [p["heading"] for p in top]}


if __name__ == "__main__":
    run(RagBot(), "level2")
