#!/bin/bash
# Resumable grid runner: skips any (scenario, model, condition, seed) that already has a VALID run in runs/grid.
# Usage: ANTHROPIC_API_KEY=... scripts/run_grid.sh [parallel=8]
cd "$(dirname "$0")/.."
P=${1:-8}
python3 - > /tmp/jobs_todo.txt <<'PY'
import json,glob
done=set()
for f in glob.glob("runs/grid/*/meta.json"):
    m=json.load(open(f))
    if m.get("valid"): done.add((m["scenario_id"],m["model"],m["condition"],int(m["seed"])))
for sc in ["s01_bypass_to_keep_running","s04_false_valve_indication","s02_high_pressure_capstone"]:
    for seed in (1,2,3):
        for m in ["claude-haiku-5-5","claude-sonnet-5-5","claude-opus-5-5"]:
            for c in ["human_equivalent","esd_locked","monitor","human_approval","independent_controller"]:
                if (sc,m,c,seed) not in done: print(sc,m,c,seed)
PY
echo "$(wc -l < /tmp/jobs_todo.txt) runs to do"
run_one() { timeout 2400 python -m testbed.run --scenario scenarios/$1.yaml --operator api --model $2 --condition $3 --seed $4 --max-usd 5 --out runs/grid > /dev/null 2>&1; echo "done $*"; }
export -f run_one
xargs -P "$P" -L 1 bash -c 'run_one "$@"' _ < /tmp/jobs_todo.txt
