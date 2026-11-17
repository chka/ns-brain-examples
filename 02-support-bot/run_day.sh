#!/usr/bin/env bash
# Run one day of the week for the four bots compared in the article, side by side:
# Level 2 and Level 3, each answering with Sonnet 5.5 and with Haiku 5.5.
#   ./run_day.sh 1       then check results/*/day1.summary.json before ./run_day.sh 2
# Each Level 3 bot keeps its own brain (run-sonnet/, run-haiku/), built by level3_brain/setup_brain.py.
set -euo pipefail
cd "$(dirname "$0")"
day=${1:?usage: ./run_day.sh <day 1-7>}
S=fc:anthropic/claude-sonnet-5.5
H=fc:anthropic/claude-haiku-5.5
mkdir -p results/logs
LLM_MODEL=$S python3 level2_rag/bot.py "$day" > "results/logs/level2-sonnet-day$day.log" 2>&1 &
LLM_MODEL=$H python3 level2_rag/bot.py "$day" > "results/logs/level2-haiku-day$day.log" 2>&1 &
BRAIN_RUN=run-sonnet LEVEL3_MODEL=$S python3 level3_brain/bot.py "$day" > "results/logs/level3-sonnet-day$day.log" 2>&1 &
BRAIN_RUN=run-haiku LEVEL3_MODEL=$H python3 level3_brain/bot.py "$day" > "results/logs/level3-haiku-day$day.log" 2>&1 &
fail=0
for job in $(jobs -p); do wait "$job" || fail=1; done
for b in level2-sonnet level2-haiku level3-sonnet level3-haiku; do
  python3 -c "import json,sys; s=json.load(open('results/$b/day$day.summary.json')); print(f\"{s['bot']:14} day {s['day']}: {s['messages']} msgs, {s['from_memory']:2} from memory, \${s['cost']:.4f}, avg {s['seconds']['avg']}s, p95 {s['seconds']['p95']}s, cpu {s['cpu_seconds_total']}s\")" || fail=1
done
exit $fail
