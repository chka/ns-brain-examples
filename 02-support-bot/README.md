# 02-support-bot: three ways to build a shop's support chatbot

One invented clothing shop (Kestrel Row), one handbook of about 34,000 tokens, a week of
pretend customers (50 messages a day, 350 in all, 45 customers, many of them coming back)
and three bots that answer them. Every bot gets exactly the same messages in the same order.

| Level | Folder | How it answers |
|---|---|---|
| 1, basic | [level1_basic](level1_basic/) | The whole handbook goes to the model with every message. No caching. |
| 2, good | [level2_rag](level2_rag/) | Retrieval (RAG): the five closest handbook passages, plus prompt caching. |
| 3, lean expert | [level3_brain](level3_brain/) | ns-brain instead of RAG, a local classifier, answers from memory without the model, and a shop that remembers its customers. |

RESULTS_TABLE

## Run it

```bash
curl -fsSL https://brain.fightclub.pro/install.sh | sh        # ns-brain, for Level 3
pip install openai sentence-transformers                       # torch comes with it
export LLM_API_KEY=...                                         # your gateway key
export LLM_BASE_URL=https://api.fightclub.pro/v1               # the default: Ringside
python3 level1_basic/bot.py 1          # day 1 only: every day costs the same at this level
python3 level2_rag/build_index.py && python3 level2_rag/bot.py
python3 level3_brain/setup_brain.py && python3 level3_brain/bot.py
```

Each run writes `results/<level>.jsonl` (every message with its answer, time, tokens and cost)
and `results/<level>.summary.json` (totals and one line per day).

Any OpenAI-compatible gateway works: point `LLM_BASE_URL` at it and set `LLM_MODEL`,
`EMBED_MODEL` and the prices in `common.py`. We ran it on Ringside with
`fc:anthropic/claude-sonnet-5.5` at every level.

Level 3 builds its brain in `run/` (ignored by git) with every ns-brain path pinned there, so
nothing touches a brain of your own. Run `setup_brain.py` again for a clean brain before each
run of `bot.py`: the bot learns as it goes, and a second run on a brain that has already seen
the traffic would answer far more from memory than a fair first run.

## What is in each folder

- `shop/`: the fact sheet (the source of truth), `make_handbook.py` that wrote the handbook
  from it, `handbook.md`, `orders.json` (the shop's order system, only Level 3 reads it) and
  `traffic/day1.json` to `day7.json` (the customers). Day 1 was written by hand;
  `make_traffic.py` wrote days 2 to 7, and the whole week was frozen before any bot ran.
- `common.py`: the LLM client, prices, and the harness that feeds the week to a bot.

## Read the numbers with these in mind

- Levels 1 and 2 get no customer data. They are built the way such bots usually are; Level
  3's order history and preferences are part of what is being compared.
- Level 3's two thresholds (`NEAR`, `SAME` in `level3_brain/bot.py`) and its follow-up rules
  were tuned on day 1. Days 2 to 7 were written afterwards and never used for tuning.
- Building the brain is a one-off model pass over the handbook (about $0.80); the RAG index
  is a one-off embedding pass (under $0.001). Both are reported apart from the chats.
- Sonnet 5.5 reasons before it answers and the reasoning is billed as output, about 300
  tokens a call at every level. The gateway did not let us turn it off.
- When ns-brain refuses a learned question because the brain already holds it, the bot keeps
  the customer's wording as another way to ask that question. Those wordings live in the bot
  process, not in the brain, so they are lost when the bot restarts.
