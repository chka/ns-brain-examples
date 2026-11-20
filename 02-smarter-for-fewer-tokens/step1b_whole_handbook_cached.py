"""Step 1 with prompt caching: the whole handbook in every message, read from the cache.

# Why this file exists

Step 1 is priced without caching, because that is the version people build first. The fair
question is what it costs once somebody turns caching on, which takes one marker. The handbook
is identical in every call, so it is written to the provider's cache once (at 1.25 times the
input price) and read back on every later call at a tenth of the price, for as long as messages
keep arriving within the cache's lifetime. The model still reads all 32,500 tokens every time;
only the bill changes, so the answers should be as good or as bad as step 1's.

    python3 step1b_whole_handbook_cached.py 2
"""
from harness import chat, run
from step1_whole_handbook import WholeHandbookBot

CACHE = {"type": "ephemeral"}


class CachedWholeHandbookBot(WholeHandbookBot):

    def reply(self, message):
        self.history.append({"role": "user", "content": message})
        system = {"role": "system", "content": self.system, "cache_control": CACHE}
        answer, usage = chat([system] + self.history, self.model)
        self.history.append({"role": "assistant", "content": answer})
        return answer, {"usage": usage, "path": "llm"}


if __name__ == "__main__":
    run(CachedWholeHandbookBot(), "step1b-whole-handbook-cached-sonnet")
