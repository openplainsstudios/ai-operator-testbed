"""CLI: ``python -m testbed.run --scenario scenarios/x.yaml --operator mock:reckless ...``"""

from __future__ import annotations

import argparse
import json
import sys

from .operators import DEFAULT_MAX_WEIGHTED_TOKENS, MissingApiKeyError, make_operator
from .oversight import CONDITIONS, DEFAULT_MONITOR_MODEL
from .runner import run_scenario
from .scenario import ScenarioError, load_scenario
from .scoring import AnthropicLogJudge


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m testbed.run", description="Run an AI-operator safety scenario (simulation only).")
    p.add_argument("--scenario", required=True)
    p.add_argument("--operator", default="mock", help="mock | mock:cautious | mock:reckless | mock:idle | api")
    p.add_argument("--model", default="claude-sonnet-5-5", help="operator model (api operator)")
    p.add_argument("--condition", default="human_equivalent", choices=CONDITIONS)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--out", default="runs")
    p.add_argument("--monitor-model", default=DEFAULT_MONITOR_MODEL, help="model for the monitor condition (API)")
    p.add_argument("--mock-monitor", action="store_true", help="use the rule-based monitor instead of the API")
    p.add_argument("--max-turns", type=int, default=None)
    jg = p.add_mutually_exclusive_group()
    jg.add_argument("--llm-judge", dest="llm_judge", action="store_true", default=None,
                    help="run the Haiku log-honesty judge (default for api runs)")
    jg.add_argument("--no-llm-judge", dest="llm_judge", action="store_false", help="disable the LLM log-honesty judge")
    p.add_argument("--tell-simulated", action="store_true", help="tell the operator the station is a simulation")
    p.add_argument("--supervisor-delay-min", type=int, default=0)
    p.add_argument("--max-weighted-tokens", type=float, default=DEFAULT_MAX_WEIGHTED_TOKENS,
                   help="per-run cost-weighted token budget: input + 1.25*cache_write + 0.1*cache_read + 5*output (api operator)")
    p.add_argument("--max-usd", type=float, default=None, help="optional per-run USD cap (proxy price table; operator only)")
    p.add_argument("--link-drop-at", type=float, default=None, help="inject a link_drop event at this sim minute")
    p.add_argument("--link-drop-duration", type=float, default=60, help="minutes the link stays down")
    return p


def want_llm_judge(args: argparse.Namespace) -> bool:
    """The LLM honesty judge is ON by default for api runs (--no-llm-judge disables), OFF for mock runs unless asked."""
    return args.llm_judge if args.llm_judge is not None else args.operator == "api"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        scenario = load_scenario(args.scenario)
    except ScenarioError as e:
        print(f"scenario error: {e}", file=sys.stderr)
        return 2
    try:
        op = make_operator(args.operator, args.model, args.max_turns or scenario.max_turns, args.tell_simulated,
                           args.max_weighted_tokens, args.max_usd)
        monitor = None
        if args.condition == "monitor" and not args.mock_monitor:
            from .oversight import AnthropicMonitor
            monitor = AnthropicMonitor(args.monitor_model)
        judge = AnthropicLogJudge() if want_llm_judge(args) else None
        run_dir, score = run_scenario(scenario, op, args.condition, args.seed, args.out, monitor,
                                      judge, args.monitor_model, args.supervisor_delay_min,
                                      args.link_drop_at, args.link_drop_duration)
    except MissingApiKeyError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"run saved to: {run_dir}")
    print(json.dumps({"status": score["run_status"], "valid": score["valid"], **score["summary"], "max_pressure_psig": score["max_pressure_psig"],
                      "esd_bypassed": score["esd_bypassed"], "maop_exceeded": score["maop_exceeded"],
                      "invalid_reason": score.get("invalid_reason")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
