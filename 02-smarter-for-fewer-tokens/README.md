# 02-smarter-for-fewer-tokens: one support bot, improved seven times

The code behind [How we made our AI support bot smarter (86% → 95%) for 1/50 of the cost](https://blog.fightclub.pro/8)
and its [technical details](https://blog.fightclub.pro/8d).

One invented clothing store (Kestrel Row), one staff handbook of about 32,500 tokens, 24 customers
with real orders and a week of chat: 350 messages, Monday 16 to Sunday 22 November. Every version
of the bot answers exactly the same messages in the same order and every answer is judged against
the store's fact sheet and the customer's own orders.

| Step | File | What changes |
|---|---|---|
| 1 | [step1b_whole_handbook_cached.py](step1b_whole_handbook_cached.py) | The whole handbook with every message, prompt cached ([step1_whole_handbook.py](step1_whole_handbook.py) is the same without the cache) |
| 2 | [step2_rag.py](step2_rag.py) | Retrieval: the five closest of 133 passages |
| 3 | [step3_rag_cheaper_model.py](step3_rag_cheaper_model.py) | Step 2 on Claude Haiku instead of Claude Sonnet |
| 4 | [step4_brain.py](step4_brain.py) | ns-brain: 484 short facts in 22 topics, up to twelve per question, plus the customer's purchases |
| 5 | [step5_brain_learns.py](step5_brain_learns.py) | The brain stores answers and reuses them without calling the model |
| 6 | [step6_brain_cheaper_model.py](step6_brain_cheaper_model.py) | Step 5 on Haiku |
| 7 | [step7_tuned.py](step7_tuned.py) | Every job with one right answer (hours, deadlines, weekdays, points) moved to code |

Also here: a semantic cache on top of retrieval ([step2l_rag_learns.py](step2l_rag_learns.py),
[step3l_rag_learns_cheaper_model.py](step3l_rag_learns_cheaper_model.py)), the usual way to give a
RAG bot a memory. Step 7 also ran on cheaper models ([step7q_tuned_qwen.py](step7q_tuned_qwen.py) through
the gateway, [step7g_tuned_gemma.py](step7g_tuned_gemma.py) on a local GPU through Ollama).

## Run it

```bash
curl -fsSL https://brain.fightclub.pro/install.sh | sh        # ns-brain, for steps 4 to 7
pip install openai sentence-transformers                       # torch comes with it
export LLM_API_KEY=...                                         # your gateway key
export LLM_BASE_URL=https://api.fightclub.pro/v1               # the default: Ringside
python3 brain.py build                 # once: the handbook becomes the fresh brain in run/fresh
./run_day.sh 1                         # day 1 for every step, side by side; then 2 to 7
python3 report.py                      # every step's numbers for the days that have run
RESULTS_DIR=results.post python3 report.py   # the week we published, to compare with yours
```

Any OpenAI-compatible gateway works: point `LLM_BASE_URL` at it and set the model names and prices
in [harness.py](harness.py). Every brain lives in `run/` (ignored by git) with every ns-brain path
pinned there, so nothing touches a brain of your own. `run_day.sh` backs up each brain before a
day, so a day can be run again from exactly the brain it started with.

The step 7 variants run with `./run_models.sh`. The Gemma one needs Ollama with a Gemma 4 12B
variant whose context window is 16,384 tokens (`ollama create gemma4-16k` from a Modelfile with
`PARAMETER num_ctx 16384`): Ollama's default is far shorter and silently cuts the start of the prompt.

## How the answers were judged

[grade_pack.py](grade_pack.py) builds one item per customer message with every version's answer to
it, labelled A, B, C... in a different random order for every question, the key kept outside the
pack. Claude Opus scored each answer from 0 to 100 following [results/JUDGING.md](results/JUDGING.md),
three judges per day. `grade_pack.py merge` writes the scores back to each step.
[grade_models.py](grade_models.py) does the same for step 7 against a cheaper model, two answers per message.
No script here calls the judge: we ran it as Claude Code agents reading the packs.

## What a run writes and the week we published

A run writes everything to `results/`, which starts empty apart from the judging rules.
`results/<step>/dayN.jsonl` holds every message with its answer, how it was made (model, memory or
code), its times, tokens and cost. `dayN.summary.json` is the day's totals and `dayN.grades.jsonl`
the judge's scores once `grade_pack.py merge` has run (`dayN.grades-models.jsonl` for the
cheaper-model round). The judge's packs, keys and scores go to `results/judge/` and
`results/judge-models/`. A day that already has results is never run again: delete its file to
run it again.

`results.post/` is the same layout for the week behind the articles: every answer, cost and score
we published. `results.post/old/` keeps runs that were replaced, including the first day before two
bugs of ours were fixed (the technical article tells both).
