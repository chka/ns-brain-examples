"""Step 3: the same RAG bot on a cheaper model. The obvious saving, and it makes the bot worse.

# The idea

Step 2 is already far cheaper than step 1, so the next lever everybody reaches for is the model.
Claude Haiku 5.5 costs a twentieth of Claude Sonnet 5.5 per token. Nothing else changes: the
same passages, the same prompt, the same cache. This file is step 2 with one line different.

# Why it is in the article

Because it fails. A smaller model given the same five passages makes more mistakes: it misreads
an exception, picks the wrong passage's number or answers a slightly different question. The
bill drops and so does the share of answers that are right. The rest of the article is about
getting the cheap model's price without paying for it in errors.

    python3 step3_rag_cheaper_model.py 1
"""
from harness import HAIKU, run
from step2_rag import RagBot


class CheapRagBot(RagBot):
    model = HAIKU


if __name__ == "__main__":
    run(CheapRagBot(), "step3-rag-haiku")
