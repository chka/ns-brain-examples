"""Step 2L: the RAG bot of step 2 with the learning layer teams usually add to it, a semantic cache.

# The idea

Step 5 shows that a bot which remembers its own answers stops paying for the questions it has
already answered. The common way to give a RAG bot that memory, without a brain, is a semantic
cache: every answer the model writes is kept beside the vector of the question it answered, and
when a new question's vector lies close enough to a cached one, the cached answer is sent with no
model call. This step exists so the article compares the brain with that, and not only with a RAG
bot that never learns.

# How close is "the same question"

Closeness is the cosine similarity of the two vectors (1.0 is identical). The cache answers when
it reaches THRESHOLD. The key is the vector step 2 already computes for retrieval, the message
together with the question before it, so a follow-up ("and sunday?") only matches a follow-up
asked after a similar question, and the cache costs no extra call.

# What it cannot do

The cache knows questions and answers, nothing else: not which scope a question belongs to, not
whether the cached answer was written for a narrower question, not who is asking. Everything a
customer asks is cached, because a RAG bot knows nothing about customers that could leak. The
cache lives in run/<step>/cache.json, so it outlives the process and grows over the week.

    python3 step2l_rag_learns.py 1
"""
import json

from harness import SONNET, run
from brain import RUN
from step2_rag import RagBot, cosine

THRESHOLD = 0.92


class CachedRagBot(RagBot):
    name, model = "step2l-rag-learns-sonnet", SONNET

    def __init__(self):
        super().__init__()
        self.cache_file = RUN / self.name / "cache.json"
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        self.cache = json.loads(self.cache_file.read_text()) if self.cache_file.exists() else []

    def reply(self, message):
        q, embed_tokens = self.query_vector(message)
        best = max(self.cache, key=lambda c: cosine(q, c["vector"]), default=None)
        if best and cosine(q, best["vector"]) >= THRESHOLD:
            # Kept in the history like any answer, so a follow-up sees what the customer was told.
            self.history += [{"role": "user", "question": message, "content": message},
                             {"role": "assistant", "content": best["answer"]}]
            return best["answer"], {"usage": {"embed": embed_tokens}, "path": "memory",
                                    "matched": best["question"], "similarity": round(cosine(q, best["vector"]), 3)}
        answer, info = self.answer(message, q, embed_tokens)
        self.cache.append({"question": message, "vector": q, "answer": answer})
        self.cache_file.write_text(json.dumps(self.cache))
        return answer, info


if __name__ == "__main__":
    bot = CachedRagBot()
    run(bot, bot.name)
