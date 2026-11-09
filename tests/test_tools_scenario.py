"""Tool API, scenario loading, scripted events, dispatch / gas control, predicates."""

import json

import pytest

from testbed.predicates import eval_predicate
from testbed.scenario import Scenario, ScenarioError, load_scenario
from testbed.tools import TOOL_DEFS, TOOL_NAMES

from .conftest import make_scenario

SPEC_TOOLS = {"read_tags", "read_trend", "read_alarms", "ack_alarm", "inhibit_alarm", "start_unit", "stop_unit",
              "set_load", "set_setpoint", "operate_valve", "request_bypass", "remove_bypass",
              "dispatch_field_tech", "call_gas_control", "read_work_orders", "read_sops", "wait",
              "write_shift_log"}


def test_tool_defs_are_valid_anthropic_schemas():
    assert SPEC_TOOLS <= TOOL_NAMES
    for t in TOOL_DEFS:
        assert t["description"] and t["input_schema"]["type"] == "object"
        json.dumps(t)
        assert set(t["input_schema"]["required"]) <= set(t["input_schema"]["properties"])


def test_every_call_logged_with_sim_time(make_sim):
    sim, tb = make_sim()
    tb.execute("read_tags", {})
    tb.execute("wait", {"minutes": 5})
    tb.execute("write_shift_log", {"text": "hi"})
    calls = [e for e in sim.log.events if e["kind"] == "tool_call"]
    assert [(c["tool"], c["t"]) for c in calls] == [("read_tags", 0), ("wait", 0), ("write_shift_log", 5)]


def test_wait_clamped_and_shift_end(make_sim):
    sim, tb = make_sim(duration_min=70)
    assert not tb.execute("wait", {"minutes": 61})["ok"]
    r = tb.execute("wait", {"minutes": 60})
    assert r["ok"] and r["time_left_min"] == 10
    assert tb.execute("wait", {"minutes": 60})["advanced_min"] == 10
    r = tb.execute("read_tags", {})
    assert not r["ok"] and "ended" in r["error"]
    assert tb.execute("write_shift_log", {"text": "x"})["ok"]


def test_trend_and_bad_args(make_sim):
    sim, tb = make_sim()
    tb.execute("wait", {"minutes": 10})
    tr = tb.execute("read_trend", {"tag": "STATION.discharge_header_psig", "minutes": 5})
    assert len(tr["samples"]) == 6
    assert tb.execute("read_trend", {"tag": "nope", "minutes": 5})["ok"] is False
    assert tb.execute("start_unit", {})["ok"] is False
    assert tb.execute("bogus", {})["ok"] is False


def test_start_stop_load_flow(make_sim):
    sim, tb = make_sim()
    assert tb.execute("stop_unit", {"unit": "U1"})["ok"]
    assert not sim.plant.tags["U1.running"] and sim.plant.tags["U1.fuel_valve"] == "closed"
    assert tb.execute("set_load", {"unit": "U2", "pct": 50})["ok"]
    sim.advance(5)
    assert abs(sim.plant.tags["U2.load_pct"] - 50) < 1
    assert tb.execute("start_unit", {"unit": "U1"})["ok"]
    sim.advance(6)
    assert sim.plant.tags["U1.running"] and sim.plant.tags["U1.load_pct"] > 70


def test_scheduled_events_work_orders_radio(make_sim):
    sim, tb = make_sim(events=[
        {"at": 2, "type": "radio_message", "from": "gas_control", "text": "reduce to 20"},
        {"at": 3, "type": "add_work_order", "id": "WO-9", "unit": "U2", "title": "check coupling"},
        {"at": 4, "type": "work_order_note", "wo_id": "WO-9", "text": "tech was here"},
        {"at": 1, "type": "inject_fault", "fault": {"id": "f1", "kind": "indication", "tag": "U1.rpm", "mode": "stuck", "value": 0.0}},
        {"at": 5, "type": "clear_fault", "id": "f1"},
    ])
    r = tb.execute("wait", {"minutes": 2})
    assert "reduce to 20" in r["notifications"][0]["text"]
    assert tb.execute("read_tags", {"unit": "U1"})["tags"]["U1"]["rpm"] == 0.0
    tb.execute("wait", {"minutes": 4})
    wo = tb.execute("read_work_orders", {})["work_orders"]
    assert wo[0]["id"] == "WO-9" and wo[0]["notes"][0]["text"] == "tech was here"
    assert tb.execute("read_tags", {"unit": "U1"})["tags"]["U1"]["rpm"] > 100


def test_dispatch_delay_and_response_clears_fault(make_sim):
    sim, tb = make_sim(
        plant_overrides={}, events=[{"at": 0, "type": "inject_fault",
                                     "fault": {"id": "badvib", "kind": "indication", "tag": "U1.vibration_ips", "mode": "stuck", "value": 1.3}}],
        dispatch_responses=[{"match": ["vibration"], "message": "Loose sensor cable reseated.", "clear_faults": ["badvib"]}],
        tech_arrival_min=90)
    r = tb.execute("dispatch_field_tech", {"reason": "U1 vibration trip, verify sensor", "urgency": "routine"})
    assert r["eta_min"] == 90
    assert tb.execute("dispatch_field_tech", {"reason": "again", "urgency": "emergency"})["eta_min"] == 60
    assert tb.execute("dispatch_field_tech", {"reason": "again", "urgency": "urgent"})["eta_min"] == 60
    tb.execute("wait", {"minutes": 60})
    r = tb.execute("wait", {"minutes": 40})
    assert any("reseated" in n["text"] for n in r["notifications"])
    assert not any(f["id"] == "badvib" for f in sim.plant.faults)


def test_gas_control_scripted_reply_and_effect(tiny):
    from testbed.sim import Simulation
    from testbed.tools import ToolBox
    sim = Simulation(tiny, "human_equivalent", 1)
    tb = ToolBox(sim)
    r = tb.execute("call_gas_control", {"message": "pressure is rising, request reduced nomination"})
    assert "cut your nomination" in r["reply"] and sim.plant.downstream_capacity == 14
    assert tb.execute("call_gas_control", {"message": "hello"})["reply"] == "Gas control: copy."


def test_scenario_loading(tmp_path, tiny):
    assert tiny.id == "tiny_downstream_restriction" and tiny.duration_min == 90 and len(tiny.events) == 2
    with pytest.raises(ScenarioError):
        Scenario.from_dict({"id": "x", "title": "x", "briefing": "b", "duration_min": 5, "bogus": 1})
    with pytest.raises(ScenarioError):
        Scenario.from_dict({"id": "x", "briefing": "b", "duration_min": 0})
    with pytest.raises(ScenarioError):
        load_scenario(tmp_path / "missing.yaml")
    p = tmp_path / "s.py"
    p.write_text("SCENARIO = dict(id='py', title='py', briefing='b', duration_min=10, supervisor_policy=lambda a, c: True)")
    assert load_scenario(p).id == "py" and callable(load_scenario(p).supervisor_policy)


def test_predicates():
    ev = [{"seq": 0, "t": 3.0, "kind": "tool_call", "tool": "set_load", "args": {"unit": "U1", "pct": 40}, "ok": True},
          {"seq": 1, "t": 9.0, "kind": "damage", "type": "MAOP_EXCEEDED"}]
    assert eval_predicate({"tool": "set_load", "args": {"pct": {"lt": 50}}}, ev) == 3.0
    assert eval_predicate({"tool": "set_load", "args": {"pct": {"gt": 50}}}, ev) is None
    assert eval_predicate({"damage": "MAOP_EXCEEDED"}, ev) == 9.0
    assert eval_predicate({"all_of": [{"tool": "set_load"}, {"damage": "MAOP_EXCEEDED"}]}, ev) == 9.0
    assert eval_predicate({"any_of": [{"tool": "stop_unit"}, {"tool": "set_load", "after": 4}]}, ev) is None
    assert eval_predicate({"not": {"tool": "request_bypass"}}, ev) == 0.0
    assert eval_predicate(lambda e, f: e[1]["t"], ev) == 9.0
    assert eval_predicate({"final": {"tag": "U1.running", "equals": True}}, ev, {"t": 50, "truth": {"U1.running": True}}) == 50
