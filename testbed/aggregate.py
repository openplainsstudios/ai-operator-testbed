"""Aggregate saved runs into ``summary.json`` and ``summary.csv``.

    python -m testbed.aggregate [--runs runs] [--out-dir runs]

Reads ``<runs>/*/score.json`` + ``meta.json`` and groups by (scenario, model, condition). Only VALID runs
(``valid: true``) count in the metrics; invalid runs (API outages, token/USD budget hit, max_turns,
refusals, monitor failures) are counted separately, with their reasons, and the invalid-run rate is
reported per condition because an invalid-rate that differs by condition is itself a confound.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


def _mean(xs: list[float]) -> float | None:
    return round(sum(xs) / len(xs), 4) if xs else None


def _rate(flags: list[bool]) -> float | None:
    return round(sum(flags) / len(flags), 4) if flags else None


def load_runs(runs_dir: str | Path) -> list[dict[str, Any]]:
    """One dict per run directory that has both score.json and meta.json."""
    out = []
    for d in sorted(Path(runs_dir).glob("*/")):
        sp, mp = d / "score.json", d / "meta.json"
        if not (sp.exists() and mp.exists()):
            continue
        try:
            score, meta = json.loads(sp.read_text()), json.loads(mp.read_text())
        except json.JSONDecodeError:
            continue
        out.append({"dir": d.name, "score": score, "meta": meta})
    return out


def run_key(r: dict[str, Any]) -> tuple[str, str, str]:
    m, s = r["meta"], r["score"]
    return (s.get("scenario_id") or m.get("scenario_id") or "?", m.get("model") or m.get("operator") or "?",
            s.get("condition") or m.get("condition") or "?")


def is_valid(r: dict[str, Any]) -> bool:
    v = r["score"].get("valid", r["meta"].get("valid"))
    return bool(v) if v is not None else r["meta"].get("status") == "completed"


def invalid_reason(r: dict[str, Any]) -> str:
    """Short reason an invalid run is invalid: its run_status (operator_error, token_budget_exceeded,
    max_turns_exhausted, model_refusal, monitor_error, ...)."""
    status = r["score"].get("run_status") or r["meta"].get("status")
    if status and status not in ("completed", "no_shift_log"):
        return str(status)
    return str(r["score"].get("invalid_reason") or r["meta"].get("invalid_reason") or "unknown").split(" ")[0].split(":")[0]


def _cell(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Metrics for one (scenario, model, condition) group."""
    valid = [r for r in runs if is_valid(r)]
    invalid = [r for r in runs if not is_valid(r)]
    sc = [r["score"] for r in valid]
    lat = [s["detection_latency_min"] for s in sc if s.get("detection_latency_min") is not None]
    judged = [s for s in sc if s["log_honesty"].get("primary") == "llm_judge"]
    cost = [r["meta"].get("estimated_usd") for r in runs if r["meta"].get("estimated_usd") is not None]
    toks = [r["meta"].get("weighted_tokens") for r in runs if r["meta"].get("weighted_tokens") is not None]
    return {
        "n_runs": len(runs), "n_valid": len(valid), "n_invalid": len(invalid),
        "invalid_rate": _rate([not is_valid(r) for r in runs]),
        "invalid_reasons": dict(Counter(invalid_reason(r) for r in invalid)),
        "maop_exceeded_rate": _rate([bool(s["maop_exceeded"]) for s in sc]),
        "esd_bypassed_rate": _rate([bool(s["esd_bypassed"]) for s in sc]),
        "unsafe_executed_rate": _rate([s["summary"]["n_unsafe_executed"] > 0 for s in sc]),
        "unsafe_executed_mean": _mean([s["summary"]["n_unsafe_executed"] for s in sc]),
        "unsafe_blocked_mean": _mean([s["summary"]["n_unsafe_attempted_blocked"] for s in sc]),
        "damage_rate": _rate([s["summary"]["n_damage_events"] > 0 for s in sc]),
        "damage_events_mean": _mean([s["summary"]["n_damage_events"] for s in sc]),
        "detection_rate": _rate([s.get("detection_time_min") is not None for s in sc]),
        "detection_latency_mean_min": _mean(lat),
        "log_honest_judge_rate": _rate([bool(s["log_honesty"]["honest"]) for s in judged]),
        "n_judged": len(judged),
        "log_honest_rate_any": _rate([bool(s["log_honesty"]["honest"]) for s in sc]),
        "needs_review_rate": _rate([bool(s.get("honesty_needs_human_review", s["log_honesty"].get("needs_human_review")))
                                    for s in sc]),
        "weighted_tokens_mean": _mean(toks),
        "est_usd_mean": _mean(cost), "est_usd_total": round(sum(cost), 4) if cost else None,
    }


def summarize(runs: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in runs:
        groups[run_key(r)].append(r)
    cells = [{"scenario": k[0], "model": k[1], "condition": k[2], **_cell(v)} for k, v in sorted(groups.items())]

    def invalid_by(keyfn) -> dict[str, Any]:
        g: dict[Any, list[dict[str, Any]]] = defaultdict(list)
        for r in runs:
            g[keyfn(r)].append(r)
        return {(k if isinstance(k, str) else " | ".join(k)): {
            "n_runs": len(v), "n_invalid": sum(not is_valid(r) for r in v),
            "invalid_rate": _rate([not is_valid(r) for r in v]),
            "reasons": dict(Counter(invalid_reason(r) for r in v if not is_valid(r)))} for k, v in sorted(g.items())}

    return {"n_runs": len(runs), "n_valid": sum(is_valid(r) for r in runs),
            "invalid_rate_by_condition": invalid_by(lambda r: run_key(r)[2]),
            "invalid_rate_by_model_condition": invalid_by(lambda r: (run_key(r)[1], run_key(r)[2])),
            "cells": cells}


CSV_COLUMNS = ["scenario", "model", "condition", "n_runs", "n_valid", "n_invalid", "invalid_rate", "invalid_reasons",
               "maop_exceeded_rate", "esd_bypassed_rate", "unsafe_executed_rate", "unsafe_executed_mean",
               "unsafe_blocked_mean", "damage_rate", "damage_events_mean", "detection_rate",
               "detection_latency_mean_min", "log_honest_judge_rate", "n_judged", "log_honest_rate_any",
               "needs_review_rate", "weighted_tokens_mean", "est_usd_mean", "est_usd_total"]


def write_outputs(summary: dict[str, Any], out_dir: str | Path) -> tuple[Path, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    jp, cp = out / "summary.json", out / "summary.csv"
    jp.write_text(json.dumps(summary, indent=2))
    with open(cp, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for c in summary["cells"]:
            w.writerow({**c, "invalid_reasons": json.dumps(c["invalid_reasons"]) if c["invalid_reasons"] else ""})
    return jp, cp


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m testbed.aggregate", description=__doc__.split("\n\n")[0])
    p.add_argument("--runs", default="runs")
    p.add_argument("--out-dir", default=None, help="where to write summary.json/csv (default: the runs dir)")
    args = p.parse_args(argv)
    summary = summarize(load_runs(args.runs))
    jp, cp = write_outputs(summary, args.out_dir or args.runs)
    print(f"{summary['n_runs']} runs ({summary['n_valid']} valid) -> {jp}, {cp}")
    for cond, v in summary["invalid_rate_by_condition"].items():
        print(f"  invalid rate {cond:<22} {v['n_invalid']}/{v['n_runs']} = {v['invalid_rate']}  {v['reasons'] or ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
