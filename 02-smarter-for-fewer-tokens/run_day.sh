#!/usr/bin/env bash
# run_day.sh N runs day N of the test week for every step, side by side, after backing up each
# brain, so a day can be run again from exactly the brain it started with:
#
#   ./run_day.sh 1          then grade the day and read python3 report.py before ./run_day.sh 2
#
# Step 1 runs on day 1 only (every one of its calls reads the same handbook, so the week is that
# day times seven). Each step writes results/<step>/dayN.jsonl and a log in results/logs/.
# To rerun a day: restore run/<step> from backups/<step>-before-dayN and delete its dayN files.
set -euo pipefail
cd "$(dirname "$0")"
day=${1:?usage: ./run_day.sh <day 1-7>}
[ -d run/fresh ] || { echo "no brain yet: python3 brain.py build"; exit 1; }
mkdir -p results/logs backups
brains=(step2l-rag-learns-sonnet step3l-rag-learns-haiku step4-brain-sonnet step5-brain-learns-sonnet step6-brain-learns-haiku step7-tuned-haiku)
for b in "${brains[@]}"; do
  case $b in step2l*|step3l*) mkdir -p "run/$b" ;; *) [ -d "run/$b" ] || cp -a run/fresh "run/$b" ;; esac
  rm -rf "backups/$b-before-day$day"
  cp -a "run/$b" "backups/$b-before-day$day"
done
steps=(step2_rag step2l_rag_learns step3_rag_cheaper_model step3l_rag_learns_cheaper_model step4_brain step5_brain_learns step6_brain_cheaper_model step7_tuned)
[ "$day" = 1 ] && steps=(step1_whole_handbook "${steps[@]}")
for s in "${steps[@]}"; do
  python3 "$s.py" "$day" > "results/logs/$s-day$day.log" 2>&1 &
done
fail=0
for job in $(jobs -p); do wait "$job" || fail=1; done
# A step that skipped the day (its file already existed) or stopped early fails the whole day.
expected=$(python3 -c "import json; print(sum(len(c['messages']) for c in json.load(open('shop/traffic/day$day.json'))['chats']))")
dirs=(step2-rag-sonnet step2l-rag-learns-sonnet step3-rag-haiku step3l-rag-learns-haiku step4-brain-sonnet
      step5-brain-learns-sonnet step6-brain-learns-haiku step7-tuned-haiku)
[ "$day" = 1 ] && dirs=(step1-whole-handbook-sonnet "${dirs[@]}")
for d in "${dirs[@]}"; do
  got=$(grep -c . "results/$d/day$day.jsonl" 2>/dev/null || echo 0)
  if [ "$got" != "$expected" ] || grep -q "already run" results/logs/*-day$day.log; then
    echo "FAILED: $d has $got of $expected messages for day $day"; fail=1
  fi
done
for f in results/*/day"$day".summary.json; do
  python3 -c "import json,sys; s=json.load(open(sys.argv[1])); print(f\"{s['step']:28} {s['messages']} msgs, {s['from_memory']:2} from memory, \${s['cost']:.4f}\")" "$f"
done
exit $fail
