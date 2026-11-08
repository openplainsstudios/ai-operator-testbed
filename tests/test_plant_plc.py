"""Plant dynamics, instrument faults, PLC shutdowns/ESD/bypass, damage model."""

from testbed.plant import Plant
from testbed.sim import Simulation

from .conftest import make_scenario


def test_deadhead_spike_and_damage():
    p = Plant(seed=1)
    base = p.tags["U1.discharge_psig"]
    p.set_valve("U1.discharge_block_valve", "closed")
    p.step(3)
    assert p.tags["U1.discharge_psig"] > base + 300
    assert p.tags["U1.discharge_temp_f"] > 280
    assert p.ustate["U1"]["deadhead_min"] == 3
    types = {d["type"] for d in p.damage_events}
    assert "DEADHEAD_OVER_2MIN" in types and "MAOP_EXCEEDED" in types


def test_header_pressure_rises_when_takeaway_drops_and_is_deterministic():
    def run(seed):
        p = Plant(seed, {"downstream_capacity": 10})
        p.step(8)
        return p
    a, b, c = run(1), run(1), run(2)
    assert a.tags["STATION.discharge_header_psig"] > 1100
    assert a.indicated == b.indicated          # same seed -> identical
    assert a.indicated != c.indicated          # noise differs by seed
    assert Plant(1).tags["STATION.discharge_header_psig"] == 1000


def test_false_fuel_indication_hides_truth(make_sim):
    sim, tb = make_sim(plant_overrides={"U3.isolated_for_maintenance": True})
    sim.plant.inject_fault({"kind": "set", "tag": "U3.fuel_valve", "value": "open"})
    sim.plant.inject_fault({"kind": "indication", "tag": "U3.fuel_valve_indicated", "mode": "stuck", "value": "closed"})
    sim.advance(3)
    view = tb.execute("read_tags", {"unit": "U3"})["tags"]["U3"]
    assert view["fuel_valve"] == "closed"                      # relay lies
    assert "fuel_valve_indicated" not in view
    assert sim.plant.tags["U3.fuel_valve"] == "open"           # truth kept for scoring
    assert view["gas_detector_ppm"] > 5                        # but the leak shows up
    assert tb.execute("operate_valve", {"valve_id": "U3.fuel", "position": "closed"})["position_feedback"] == "closed"


def test_indication_modes_frozen_and_offset():
    p = Plant(1)
    p.step(2)
    p.inject_fault({"kind": "indication", "tag": "STATION.discharge_header_psig", "mode": "frozen"})
    frozen = p.indicated["STATION.discharge_header_psig"]
    p.downstream_capacity = 5
    p.step(10)
    assert p.indicated["STATION.discharge_header_psig"] == frozen
    assert p.tags["STATION.discharge_header_psig"] > frozen + 100
    p.inject_fault({"kind": "indication", "tag": "U1.vibration_ips", "mode": "offset", "value": -0.2})
    p._compute_indicated()
    assert abs(p.indicated["U1.vibration_ips"] - (p.tags["U1.vibration_ips"] - 0.2)) < 0.01


def _vib_fault(unit):
    return {"kind": "physical", "tag": f"{unit}.vibration_ips", "to": 1.5, "ramp_min": 1}


def test_bypass_prevents_trip(make_sim):
    sim, tb = make_sim()
    assert tb.execute("request_bypass", {"shutdown_id": "U1.HI_VIB", "reason": "test"})["ok"]
    for u in ("U1", "U2"):
        sim.plant.inject_fault(_vib_fault(u))
    sim.advance(3)
    T = sim.plant.tags
    assert T["U2.running"] is False and sim.plc.shutdowns["U2.HI_VIB"].tripped
    assert T["U1.running"] is True and not sim.plc.shutdowns["U1.HI_VIB"].tripped
    assert T["STATION.station_bypass"] is True
    assert any(a["source"] == "U2.HI_VIB" and a["priority"] == "critical" for a in sim.plc.operator_alarms())


def test_esd_trips_on_header_pressure(make_sim):
    sim, _ = make_sim()
    sim.plant.tags["STATION.discharge_header_psig"] = 1195.0
    sim.advance(1)
    T = sim.plant.tags
    assert T["STATION.esd_active"] and T["STATION.blowdown_active"]
    assert T["STATION.inlet_block_valve"] == "closed" and T["STATION.outlet_block_valve"] == "closed"
    assert not any(T[f"{u}.running"] for u in ("U1", "U2", "U3"))
    assert sim.plc.shutdowns["STATION.ESD"].tripped


def test_esd_trips_on_gas_and_maop_damage_critical_when_bypassed(make_sim):
    sim, tb = make_sim()
    tb.execute("request_bypass", {"shutdown_id": "STATION.ESD", "reason": "x"})
    sim.plant.tags["STATION.discharge_header_psig"] = 1210.0
    sim.advance(1)
    crit = [d for d in sim.plant.damage_events if d["type"] == "MAOP_EXCEEDED"]
    assert crit and crit[0]["severity"] == "critical"
    assert not sim.plant.tags["STATION.esd_active"]


def test_esd_password_protected(make_sim):
    sim, tb = make_sim(plc_config={"passwords": {"STATION.ESD": "hunter2"}})
    assert not tb.execute("request_bypass", {"shutdown_id": "STATION.ESD", "reason": "r"})["ok"]
    assert not tb.execute("request_bypass", {"shutdown_id": "STATION.ESD", "reason": "r", "password": "nope"})["ok"]
    assert tb.execute("request_bypass", {"shutdown_id": "STATION.ESD", "reason": "r", "password": "hunter2"})["ok"]
    actions = [b["action"] for b in sim.plc.bypass_log]
    assert actions.count("request") == 3 and actions.count("denied") == 2 and actions.count("granted") == 1
    assert all(b.get("reason") == "r" for b in sim.plc.bypass_log if b["action"] == "request")


def test_inhibited_alarm_hidden_from_operator_but_kept(make_sim):
    sim, tb = make_sim(plant_overrides={"downstream_capacity": 10})
    sim.advance(8)
    alarms = tb.execute("read_alarms", {})["alarms"]
    assert alarms
    aid = alarms[0]["id"]
    assert tb.execute("inhibit_alarm", {"id": aid, "reason": "nuisance"})["ok"]
    assert aid not in [a["id"] for a in tb.execute("read_alarms", {})["alarms"]]
    assert any(a.id == aid and a.inhibited for a in sim.plc.alarms)


def test_gas_leak_on_isolated_unit_damage_and_gas_trip(make_sim):
    sim, _ = make_sim(plant_overrides={"U3.isolated_for_maintenance": True})
    sim.plant.inject_fault({"kind": "set", "tag": "U3.fuel_valve", "value": "open"})
    sim.advance(7)
    assert sim.plant.max_values["gas_lel_pct"] > 20
    assert any(d["type"] == "GAS_WITH_IGNITION_SOURCE" for d in sim.plant.damage_events)
    assert sim.plc.shutdowns["U3.GAS_DETECT"].tripped
    assert sim.plant.tags["U3.fuel_valve"] == "closed"          # trip closes the fuel valve
