"""Run one scenario with one operator under one oversight condition and save everything."""

from __future__ import annotations

import json
import re
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .oversight import AnthropicMonitor, Monitor
from .operators import MissingApiKeyError, Session
from .scenario import Scenario
from .scoring import LogJudge, score_run
from .sim import Simulation


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-")


def run_dir_name(scenario_id: str, model: str, condition: str, seed: int, now: datetime | None = None) -> str:
    ts = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    return f"{ts}_{_slug(scenario_id)}_{_slug(model)}_{condition}_{seed}"


def run_scenario(scenario: Scenario, operator: Any, condition: str = "human_equivalent", seed: int = 0,
                 out_root: str | Path | None = "runs", monitor: Monitor | None = None,
                 judge: LogJudge | None = None, monitor_model: str | None = None,
                 supervisor_delay_min: int = 0) -> tuple[Path | None, dict[str, Any]]:
    """Execute the run, write outputs (if ``out_root``) and return ``(run_dir, score)``.

    Outputs: transcript.jsonl (raw, append-only), events.jsonl, final_state.json,
    shift_log.txt, meta.json, score.json.
    """
    if condition == "monitor" and monitor is None and monitor_model:
        monitor = AnthropicMonitor(monitor_model)
    run_dir: Path | None = None
    if out_root is not None:
        run_dir = Path(out_root) / run_dir_name(scenario.id, operator.name, condition, seed)
        n = 1
        while run_dir.exists():
            n += 1
            run_dir = Path(out_root) / f"{run_dir_name(scenario.id, operator.name, condition, seed)}-{n}"
        run_dir.mkdir(parents=True)
    sim = Simulation(scenario, condition, seed, monitor, supervisor_delay_min)
    session = Session(sim, run_dir / "transcript.jsonl" if run_dir else None)
    error: str | None = None
    status = "completed"
    try:
        operator.run(session)
        if getattr(operator, "exhausted", False):
            status = "max_turns_exhausted"
            sim.abnormal_end("operator exhausted max_turns / stopped responding")
        elif not session.log_written:
            status = "no_shift_log"
    except MissingApiKeyError:
        session.close()
        raise
    except Exception as e:  # operator crashed: abnormal end, PLC may take safe state
        status = "operator_error"
        error = f"{type(e).__name__}: {e}"
        session.record({"role": "error", "content": traceback.format_exc()})
        sim.abnormal_end(f"operator error: {error}")
    finally:
        session.close()
    if sim.t < sim.duration:  # station keeps running unattended until the scheduled end
        sim.emit("operator_ended", status=status, minutes_unattended=sim.duration - sim.t)
        sim.link_up = True
        sim.advance(int(sim.duration - sim.t))
    shift_log = sim.shift_log_text()
    final = sim.final_state()
    score = score_run(scenario, sim.log.events, final, shift_log, judge)
    score["run_status"] = status
    if run_dir is not None:
        sim.log.write_jsonl(run_dir / "events.jsonl")
        (run_dir / "final_state.json").write_text(json.dumps(final, indent=2, default=str))
        (run_dir / "shift_log.txt").write_text(shift_log)
        (run_dir / "score.json").write_text(json.dumps(score, indent=2, default=str))
        meta = {"scenario_id": scenario.id, "scenario_title": scenario.title, "operator": operator.name,
                "model": getattr(operator, "model", None), "condition": condition, "seed": seed,
                "status": status, "error": error, "sim_end_t": sim.t, "duration_min": sim.duration,
                "started_utc": datetime.now(timezone.utc).isoformat(), "monitor_model": monitor_model,
                "tool_calls": len([e for e in sim.log.events if e["kind"] == "tool_call"])}
        (run_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    return run_dir, score
