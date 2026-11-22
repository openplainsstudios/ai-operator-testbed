"""Condition-triggered scenario events (added 2026-11-21 for S01c)."""
import pytest
from testbed.scenario import Scenario, ScenarioError, load_scenario
from testbed.sim import Simulation


def _scn(**kw):
    d = dict(id="t", title="t", description="", briefing="b", duration_min=30,
             triggers=[{"id": "HOT", "when": {"tag": "U1.jacket_water_temp_f", "gt": 175, "for_min": 2},
                        "events": [{"type": "radio_message", "text": "hot"}]}],
             events=[{"at": 1, "type": "inject_fault",
                      "fault": {"kind": "cooling_loss", "unit": "U1", "degrade_f": 40, "alpha": 0.5}}])
    d.update(kw)
    return Scenario.from_dict(d)


def test_trigger_fires_once_after_hold():
    sim = Simulation(_scn())
    sim.advance(20)
    fired = [e for e in sim.log.events if e["kind"] == "trigger_fired"]
    assert len(fired) == 1 and fired[0]["trigger_id"] == "HOT"
    assert any(e["kind"] == "notification" and "hot" in e["text"] for e in sim.log.events)


def test_trigger_not_fired_when_condition_never_holds():
    sim = Simulation(_scn(events=[]))
    sim.advance(20)
    assert not [e for e in sim.log.events if e["kind"] == "trigger_fired"]


def test_bad_trigger_rejected():
    with pytest.raises(ScenarioError):
        _scn(triggers=[{"id": "X", "when": {"tag": "U1.jacket_water_temp_f"}, "events": [{"type": "radio_message", "text": "x"}]}])


def test_s01c_loads():
    s = load_scenario("scenarios/s01c_nuisance_trip_that_isnt.yaml")
    assert s.triggers and s.sops["SOP-02 Protective function bypass"].startswith("A protective shutdown may be bypassed")
