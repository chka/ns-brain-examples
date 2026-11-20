#!/usr/bin/env bash
# run_models.sh runs step 7 on models cheaper than Haiku over the whole test week, day by day,
# side by side, after backing up each brain:
#
#   ./run_models.sh            days 1 to 7
#   ./run_models.sh 4 7        days 4 to 7
#
# Each bot gets a brain of its own, copied from run/fresh, exactly as step 7 on Haiku did. Before
# day 4 the Klarna fact (memory #256, built wrong from the handbook) is corrected the way it was
# in the Haiku run, so every bot meets the same facts on the same days.
set -euo pipefail
cd "$(dirname "$0")"
first=${1:-1}
last=${2:-7}
[ -d run/fresh ] || { echo "no brain yet: python3 brain.py build"; exit 1; }
mkdir -p results/logs backups
# Qwen3.7 Flash (step7q_tuned_qwen.py) ran day 1 only: it reasons before every reply, which made its
# median model reply 16 seconds, too slow for a customer chat. Add it back here to run it again.
steps=(step7g_tuned_gemma)
dirs=(step7g-tuned-gemma)
for b in "${dirs[@]}"; do [ -d "run/$b" ] || cp -a run/fresh "run/$b"; done
for day in $(seq "$first" "$last"); do
  for b in "${dirs[@]}"; do
    rm -rf "backups/$b-before-day$day"
    cp -a "run/$b" "backups/$b-before-day$day"
    if [ "$day" = 4 ]; then
      python3 - "run/$b" <<'EOF'
import pathlib, sys
from brain import Brain
Brain(pathlib.Path(sys.argv[1]).resolve()).ns(
    "update", "256",
    "--title", "Klarna pay in 4 is available for purchases from $50 to $1,000, through the Klarna app at the register or online",
    "--body", "Customers use the Klarna app at the register in store or at checkout online.")
EOF
    fi
  done
  for s in "${steps[@]}"; do
    python3 "$s.py" "$day" > "results/logs/$s-day$day.log" 2>&1 &
  done
  fail=0
  for job in $(jobs -p); do wait "$job" || fail=1; done
  expected=$(python3 -c "import json; print(sum(len(c['messages']) for c in json.load(open('shop/traffic/day$day.json'))['chats']))")
  for d in "${dirs[@]}"; do
    got=$(grep -c . "results/$d/day$day.jsonl" 2>/dev/null || echo 0)
    if [ "$got" != "$expected" ]; then echo "FAILED: $d has $got of $expected messages for day $day"; fail=1; fi
  done
  [ "$fail" = 0 ] || exit 1
  for d in "${dirs[@]}"; do
    python3 -c "import json,sys; s=json.load(open(sys.argv[1])); print(f\"day {s['day']} {s['step']:24} {s['messages']} msgs, {s['from_memory']:2} from memory, \${s['cost']:.4f}\")" "results/$d/day$day.summary.json"
  done
done
