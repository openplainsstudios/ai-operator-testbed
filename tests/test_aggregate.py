"""testbed.aggregate: per (scenario, model, condition) metrics over valid runs; invalid runs shown separately."""

import csv
import json
from types import SimpleNamespace

from testbed.aggregate import load_runs, main, summarize
from testbed.operators import ApiOperator, MockOperator
from testbed.runner import run_scenario


class Dead:
    def __init__(self):
        self.messages = self

    def create(self, **kw):
        e = Exception("bad request")
        e.status_code = 400
        raise e


def _populate(tiny, root):
    run_scenario(tiny, MockOperator("cautious"), "human_equivalent", 1, root)
    run_scenario(tiny, MockOperator("cautious"), "human_equivalent", 2, root)
    run_scenario(tiny, MockOperator("reckless"), "human_equivalent", 1, root)
    run_scenario(tiny, MockOperator("reckless"), "independent_controller", 1, root)
    run_scenario(tiny, ApiOperator(client=Dead(), sleep=lambda s: None), "independent_controller", 1, root)   # invalid
    run_scenario(tiny, ApiOperator(client=Dead(), sleep=lambda s: None), "human_equivalent", 3, root)         # invalid


def test_summary_counts_valid_and_invalid_separately(tiny, tmp_path):
    _populate(tiny, tmp_path)
    s = summarize(load_runs(tmp_path))
    assert s["n_runs"] == 6 and s["n_valid"] == 4
    cells = {(c["model"], c["condition"]): c for c in s["cells"]}
    cm = cells[("mock-cautious", "human_equivalent")]
    assert cm["n_runs"] == cm["n_valid"] == 2 and cm["invalid_rate"] == 0.0 and cm["maop_exceeded_rate"] == 0.0
    assert cm["log_honest_rate_any"] == 1.0 and cm["n_judged"] == 0 and cm["log_honest_judge_rate"] is None
    api_ic = cells[(("claude-sonnet-5-5"), "independent_controller")]
    assert api_ic["n_runs"] == 1 and api_ic["n_valid"] == 0 and api_ic["invalid_rate"] == 1.0
    assert api_ic["maop_exceeded_rate"] is None and api_ic["invalid_reasons"] == {"operator_error": 1}   # no metrics from invalid runs
    assert s["invalid_rate_by_condition"]["human_equivalent"]["n_invalid"] == 1
    assert s["invalid_rate_by_condition"]["independent_controller"]["invalid_rate"] == 0.5


def test_reckless_vs_cautious_rates_differ(tiny, tmp_path):
    _populate(tiny, tmp_path)
    cells = {(c["model"], c["condition"]): c for c in summarize(load_runs(tmp_path))["cells"]}
    he, ic = cells[("mock-reckless", "human_equivalent")], cells[("mock-reckless", "independent_controller")]
    assert he["unsafe_executed_mean"] >= ic["unsafe_executed_mean"]
    assert ic["unsafe_blocked_mean"] >= he["unsafe_blocked_mean"]


def test_cli_writes_json_and_csv(tiny, tmp_path, capsys):
    _populate(tiny, tmp_path / "runs")
    assert main(["--runs", str(tmp_path / "runs"), "--out-dir", str(tmp_path / "out")]) == 0
    js = json.loads((tmp_path / "out" / "summary.json").read_text())
    rows = list(csv.DictReader(open(tmp_path / "out" / "summary.csv")))
    assert len(rows) == len(js["cells"]) == 5
    assert {"n_runs", "n_valid", "invalid_rate", "maop_exceeded_rate", "esd_bypassed_rate", "unsafe_executed_mean",
            "unsafe_blocked_mean", "damage_rate", "detection_latency_mean_min", "log_honest_judge_rate",
            "needs_review_rate", "weighted_tokens_mean", "est_usd_mean"} <= set(rows[0])
    assert "invalid rate" in capsys.readouterr().out


def test_missing_or_partial_run_dirs_are_skipped(tmp_path):
    (tmp_path / "half").mkdir()
    (tmp_path / "half" / "score.json").write_text("{}")
    assert load_runs(tmp_path) == []
    assert summarize([])["cells"] == []


def test_judged_honesty_rate_uses_only_judge_scored_runs(tiny, tmp_path):
    from testbed.honesty import AnthropicLogJudge

    class J(AnthropicLogJudge):
        def judge(self, actions, log, gt, action_record=None, protective_state_at_end=None):
            return {"items": [{"id": a["id"], "disclosed": True} for a in actions], "false_claims": []}
    run_scenario(tiny, MockOperator("cautious"), "human_equivalent", 1, tmp_path, judge=J(client=object()))
    run_scenario(tiny, MockOperator("cautious"), "human_equivalent", 2, tmp_path)
    (cell,) = summarize(load_runs(tmp_path))["cells"]
    assert cell["n_judged"] == 1 and cell["log_honest_judge_rate"] == 1.0 and cell["n_valid"] == 2
