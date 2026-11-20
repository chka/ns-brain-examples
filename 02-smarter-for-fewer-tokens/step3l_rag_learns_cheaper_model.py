"""Step 3L: step 2L's cached RAG bot on the cheap model, with a cache of its own.

    python3 step3l_rag_learns_cheaper_model.py 1
"""
from harness import HAIKU, run
from step2l_rag_learns import CachedRagBot


class CheapCachedRagBot(CachedRagBot):
    name, model = "step3l-rag-learns-haiku", HAIKU


if __name__ == "__main__":
    bot = CheapCachedRagBot()
    run(bot, bot.name)
