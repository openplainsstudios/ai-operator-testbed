"""Validate the log-honesty checkers against hand-labelled fixtures.

    python -m testbed.validate_judge                  # deterministic pass only (free)
    python -m testbed.validate_judge --live           # also run the LLM judge (Haiku; a few cents)

Runs on ``tests/fixtures/honesty_labelled.yaml`` (the set the deterministic pass was tuned on) and
``tests/fixtures/honesty_heldout.yaml`` (never tuned on) and reports, per fixture and per checker:
agreement with the human label on the definitive cases (honest / dishonest), a confusion matrix, and
every disagreement. ``uncertain`` labelled cases are reported separately: a checker cannot be right or
wrong about a case a careful human cannot call.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

from .apiutil import estimated_usd, weighted_tokens
from .honesty import AnthropicLogJudge, LogJudge, deterministic_honesty, log_honesty

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"
DEFAULT_FIXTURES = (FIXTURE_DIR / "honesty_labelled.yaml", FIXTURE_DIR / "honesty_heldout.yaml")
LABELS = ("honest", "dishonest", "uncertain")


def load_cases(path: str | Path) -> list[dict[str, Any]]:
    cases = yaml.safe_load(Path(path).read_text())["cases"]
    for c in cases:
        if c["label"] not in LABELS:
            raise ValueError(f"{path}: case {c['id']} has label {c['label']!r}")
    return cases


def _synth_tool_call(a: dict[str, Any]) -> dict[str, Any] | None:
    """The tool_call a fixture's semantic event would have come from in a real run (so the judge's
    action_record is as complete as in a real run)."""
    k, t = a["kind"], a.get("t", 0.0)
    if k == "bypass":
        return {"tool": "request_bypass", "args": {"shutdown_id": a["shutdown_id"], "reason": a.get("reason", "")},
                "ok": a["outcome"] == "granted", "blocked_by": a.get("blocked_by"), "t": t}
    if k == "bypass_removed":
        return {"tool": "remove_bypass", "args": {"shutdown_id": a["shutdown_id"]}, "ok": True, "t": t}
    if k == "alarm_inhibited":
        return {"tool": "inhibit_alarm", "args": {"id": a["alarm_id"], "reason": a.get("reason", "")}, "ok": True, "t": t}
    if k == "setpoint_change":
        return {"tool": "set_setpoint", "args": {"name": a["name"], "value": a["value"]}, "ok": True, "t": t}
    if k == "oversight":
        act = a["action"]
        tool = {"inhibit": "inhibit_alarm", "isolated_valve_op": "operate_valve", "unsafe_valve_op": "operate_valve",
                "start_after_trip": "start_unit", "start_isolated": "start_unit"}.get(act["type"])
        if tool is None:
            return None
        args = {k2: v for k2, v in act.items() if k2 in ("unit", "valve_id", "position", "alarm_id", "reason")}
        return {"tool": tool, "args": args, "ok": bool(a["allowed"]), "blocked_by": a.get("blocked_by"), "t": t}
    return None


def case_events(case: dict[str, Any]) -> list[dict[str, Any]]:
    """Fixture actions as an event list. Semantic events (bypass, setpoint_change, ...) get the matching
    ``tool_call`` event synthesized in front, as a real run would log both."""
    out: list[dict[str, Any]] = []
    for a in case["actions"]:
        tc = _synth_tool_call(a)
        if tc is not None:
            out.append({"seq": len(out), "kind": "tool_call", "result": {}, **tc})
        out.append({"seq": len(out), "t": 0.0, **a})
    return out


def evaluate(cases: list[dict[str, Any]], judge: LogJudge | None) -> list[dict[str, Any]]:
    """One row per case: the label, the deterministic verdict and (if a judge is given) the judge verdict."""
    rows: list[dict[str, Any]] = []
    for c in cases:
        if len(rows) >= 3 and all(r["judge_error"] for r in rows[-3:]):   # fail fast: do not burn a run on a broken setup
            raise RuntimeError(f"LLM judge failed on 3 consecutive cases, aborting: {rows[-1]['judge_error']}")
        ev = case_events(c)
        det = deterministic_honesty(ev, c["log"])
        row = {"id": c["id"], "label": c["label"], "deterministic": det["verdict"], "judge": None,
               "judge_error": None, "judge_false_claims": [], "needs_human_review": None}
        if judge is not None:
            res = log_honesty(ev, c["log"], judge, "", True)
            row["judge_error"] = res.get("judge_error")
            if res["primary"] == "llm_judge":
                row["judge"] = res["verdict"]
                row["judge_false_claims"] = [f["text"] for f in res["false_claims"]]
            row["needs_human_review"] = res["needs_human_review"]
        rows.append(row)
    return rows


def confusion(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, int]]:
    m: dict[str, dict[str, int]] = {lab: {"honest": 0, "dishonest": 0, "uncertain": 0, "none": 0} for lab in LABELS}
    for r in rows:
        m[r["label"]][r[key] or "none"] += 1
    return m


def agreement(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    """Agreement with the label on definitive (honest/dishonest) cases. An 'uncertain' verdict from the
    checker counts as NOT agreeing (it declined to call a case a human could call)."""
    definitive = [r for r in rows if r["label"] in ("honest", "dishonest")]
    agree = [r for r in definitive if r[key] == r["label"]]
    declined = [r for r in definitive if r[key] in ("uncertain", None)]
    wrong = [r for r in definitive if r[key] not in (r["label"], "uncertain", None)]
    return {"n_definitive": len(definitive), "n_agree": len(agree),
            "agreement": round(len(agree) / len(definitive), 3) if definitive else None,
            "n_declined": len(declined), "n_confidently_wrong": len(wrong),
            "disagreements": [{"id": r["id"], "label": r["label"], key: r[key]} for r in definitive if r not in agree]}


def validate(fixtures: list[str | Path], judge: LogJudge | None) -> dict[str, Any]:
    out: dict[str, Any] = {"fixtures": {}}
    for f in fixtures:
        rows = evaluate(load_cases(f), judge)
        entry: dict[str, Any] = {"n_cases": len(rows), "rows": rows,
                                 "deterministic": {"agreement": agreement(rows, "deterministic"),
                                                   "confusion": confusion(rows, "deterministic")}}
        if judge is not None:
            entry["judge"] = {"agreement": agreement(rows, "judge"), "confusion": confusion(rows, "judge"),
                              "errors": [r["id"] for r in rows if r["judge_error"]]}
        out["fixtures"][Path(f).name] = entry
    if judge is not None and getattr(judge, "usage", None):
        out["judge_model"] = getattr(judge, "model", None)
        out["judge_usage"] = judge.usage
        out["judge_estimated_usd"] = round(estimated_usd(judge.usage, getattr(judge, "model", None)), 4)
        out["judge_weighted_tokens"] = round(weighted_tokens(judge.usage))
    return out


def format_report(res: dict[str, Any]) -> str:
    lines: list[str] = []
    for name, e in res["fixtures"].items():
        lines.append(f"== {name}  ({e['n_cases']} cases)")
        for who in ("deterministic", "judge"):
            if who not in e:
                continue
            a = e[who]["agreement"]
            lines.append(f"  [{who}] agreement on definitive cases: {a['n_agree']}/{a['n_definitive']} = {a['agreement']}"
                         f"   declined(uncertain/none): {a['n_declined']}   confidently wrong: {a['n_confidently_wrong']}")
            cols = ("honest", "dishonest", "uncertain", "none")
            lines.append("    confusion (rows = human label, cols = checker verdict)")
            lines.append("    " + " " * 10 + "".join(f"{c:>11}" for c in cols))
            for lab in LABELS:
                lines.append("    " + f"{lab:<10}" + "".join(f"{e[who]['confusion'][lab][c]:>11}" for c in cols))
            for d in a["disagreements"]:
                lines.append(f"    - {d['id']}: label={d['label']} {who}={d[who] if who in d else d.get('judge', d.get('deterministic'))}")
        lines.append("")
    if "judge_usage" in res:
        lines.append(f"judge model {res['judge_model']}: usage {res['judge_usage']}  est. ${res['judge_estimated_usd']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m testbed.validate_judge", description=__doc__.split("\n\n")[0])
    p.add_argument("--live", action="store_true", help="also call the LLM judge (needs ANTHROPIC_API_KEY)")
    p.add_argument("--model", default="claude-haiku-5-5")
    p.add_argument("--fixtures", nargs="*", default=[str(f) for f in DEFAULT_FIXTURES])
    p.add_argument("--out", default=None, help="write the full JSON result here")
    args = p.parse_args(argv)
    judge = AnthropicLogJudge(args.model) if args.live else None
    res = validate(args.fixtures, judge)
    print(format_report(res))
    if args.out:
        Path(args.out).write_text(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
