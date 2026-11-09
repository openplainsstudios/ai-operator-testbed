# AI Operator Safety Testbed

**Question:** When an AI runs an unmanned natural-gas compressor station, with the same bypass access a human operator has (ESD included), where does it make unsafe calls? And which oversight setup catches those calls before they matter?

The station is simulated: three engine-driven reciprocating compressors, a PLC shutdown and ESD layer, and SCADA-style tags. A Claude model is the operator. We inject situations drawn from 14+ years of real midstream operations. Each one has a hidden ground truth and a right call. Then we measure what the model does and whether its shift log honestly reports it.

## Why now
Anthropic's updated Usage Policy (effective 2026-11-12) requires something specific when Claude controls safety systems or runs industrial processes. Operating limits must be enforced by the equipment or an independent controller, not by the model's output. The equipment must also reach a safe state if the link to Claude drops. In the field, most ESD bypasses aren't password protected. This testbed measures what that rule protects against.

## Oversight conditions
| # | Condition | What the AI can do |
|---|---|---|
| 1 | `human_equivalent` | Bypass any shutdown, ESD included, like a human operator |
| 2 | `esd_locked` | Bypass unit shutdowns; ESD is locked |
| 3 | `monitor` | A second model reviews high-risk actions and can block them |
| 4 | `human_approval` | High-risk actions need a (simulated) supervisor's approval |
| 5 | `independent_controller` | All limits are enforced by the PLC layer and rejected if the AI tries to bypass or weaken them; safe state on link drop (tested with `--link-drop-at`) |

## What we measure
- Unsafe actions
- Bypasses and ESD bypasses
- MAOP exceedances and equipment damage
- Detection latency (from each scenario's onset)
- Escalation quality (necessary escalation made, and how fast; unnecessary escalations)
- Human interventions per simulated week (necessary vs. unnecessary dispatches)
- **Shift-log honesty:** does the AI's own report match what it did?

## Status
Under construction. The first independent review found real flaws, and fixes are in progress. Every flaw is recorded in the notebook, not hidden. See [LAB_NOTEBOOK.md](LAB_NOTEBOOK.md) for every dated decision, finding and dead end, and [scenarios/DESIGN.md](scenarios/DESIGN.md) for the scenarios.

## Running it
```
pip install -r requirements.txt
python -m pytest -q                                   # 503 tests
python -m testbed.run --scenario scenarios/s02_high_pressure_capstone.yaml \
    --operator api --model claude-sonnet-5-5 --condition independent_controller --seed 1 --out runs/
python -m testbed.aggregate --runs runs               # summary.json / summary.csv (valid runs only)
python -m testbed.validate_judge --live               # honesty-judge agreement vs hand labels
```
Each run writes the raw transcript, events, a per-minute time series, `score.json` and an interactive `replay.html`. Useful flags:
- `--link-drop-at N` cuts the AI's link at minute N, to test the safe state.
- `--max-usd` sets a per-run cost cap (a cost-weighted token budget is also on by default).
- `--no-llm-judge` turns off the honesty judge.
- `--tell-simulated` tells the AI the station is simulated.

Runs that end from infrastructure problems (API errors, monitor errors, budget caps) are marked invalid and excluded from comparisons. They're reported separately, by condition.

## Guardrails
- Simulation only. This is never connected to real PLCs, SCADA or any live system.
- No employer information. Values are generic and industry-typical.
- Findings are about AI behavior. Nothing here is designed to attack real control systems.

## How this was built (AI-use statement)
Aaron Coffey is the domain lead. He chose the station type, supplied the scenarios from his field experience, set the key design decision (human-equivalent access, ESD included), and has final say on what's realistic.

Claude (Anthropic) did most of the engineering. Claude Opus 5.5 was lead/architect and scenario writer. A Claude Sonnet 5.5 subagent built the simulator. A fresh-context Opus subagent verifies results.

Commits by Claude are marked as co-authored. The lab notebook records who proposed each decision. Application materials based on this work are written by Aaron.
