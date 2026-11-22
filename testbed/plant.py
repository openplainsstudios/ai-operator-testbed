"""Simulated compressor station physics ("symptoms, not physics").

Truth is stored in a flat tag dict ``Plant.tags`` keyed ``"U1.discharge_psig"`` /
``"STATION.discharge_header_psig"``. What an operator *sees* is ``Plant.indicated``, which
is truth plus sensor noise plus any injected instrument faults. The PLC also acts on
indicated values (like real transmitters), while the damage model uses truth.

Units: 1 tick = 1 simulated minute. Deterministic given ``seed``.
"""

from __future__ import annotations

import math
import random
from typing import Any

UNITS = ["U1", "U2", "U3"]
MAOP_PSIG = 1200.0
NOMINAL_LOAD = 80.0
UNIT_FLOW_MMSCFD = 10.0  # per unit at 100% load
HEADER_SUBSTEPS = 4
LEAK_FUEL_FLOW_MSCFD = 15.0  # fuel flow reading on an isolated unit that is leaking

# Tags exposed to the operator per unit (``fuel_valve`` is shown as the *indicated* value).
UNIT_TAGS = [
    "running", "rpm", "load_pct", "suction_psig", "interstage_psig", "interstage_temp_f",
    "discharge_psig", "discharge_temp_f", "rod_load_pct", "vibration_ips",
    "jacket_water_temp_f", "lube_oil_psig", "fuel_valve", "fuel_valve_indicated",
    "fuel_flow_mscfd", "suction_block_valve", "discharge_block_valve",
    "isolated_for_maintenance", "gas_lel_pct",  # gas detector reading, % LEL
]
STATION_TAGS = [
    "suction_header_psig", "discharge_header_psig", "flow_mmscfd", "ambient_temp_f",
    "scrubber_level_pct", "recycle_valve_pct", "station_bypass", "esd_active",
    "blowdown_active", "inlet_block_valve", "outlet_block_valve",
]
# Tags that only make sense while the engine turns (physical faults skip them when stopped).
_RUNNING_ONLY = {"vibration_ips", "lube_oil_psig", "rod_load_pct", "rpm", "fuel_flow_mscfd"}
_NOISE_REL = {  # relative noise per tag (sigma)
    "suction_psig": 0.001, "interstage_psig": 0.001, "discharge_psig": 0.001,
    "suction_header_psig": 0.001, "discharge_header_psig": 0.001,
    "interstage_temp_f": 0.003, "discharge_temp_f": 0.003, "jacket_water_temp_f": 0.003,
    "vibration_ips": 0.02, "lube_oil_psig": 0.01, "rod_load_pct": 0.005,
    "rpm": 0.001, "fuel_flow_mscfd": 0.01, "flow_mmscfd": 0.005, "load_pct": 0.0,
}

VALVE_IDS = (
    [f"{u}.{v}" for u in UNITS for v in ("suction_block", "discharge_block", "fuel")]
    + ["STATION.inlet_block", "STATION.outlet_block", "STATION.recycle", "STATION.blowdown"]
)


def valve_tag(valve_id: str) -> str:
    """Map an operator valve id (``U1.suction_block``) to its truth tag."""
    u, v = valve_id.split(".", 1)
    m = {
        "suction_block": "suction_block_valve", "discharge_block": "discharge_block_valve",
        "fuel": "fuel_valve", "inlet_block": "inlet_block_valve",
        "outlet_block": "outlet_block_valve", "recycle": "recycle_valve_pct",
        "blowdown": "blowdown_active",
    }
    return f"{u}.{m[v]}"


class Plant:
    """Time-stepped station model with hidden truth, instrument faults and damage tracking."""

    def __init__(self, seed: int = 0, overrides: dict[str, Any] | None = None) -> None:
        self.rng = random.Random(seed)
        self.t: float = 0.0
        self.suction_supply_psig: float = 400.0
        self.ambient_base_f: float = 70.0
        self.auto_unload: bool = False
        self.header_gain: float = 1.5  # psi per minute per MMscfd of net imbalance
        self.leak_rate_per_min: float = 4.0  # % LEL per minute near an isolated unit with fuel valve open
        self.faults: list[dict[str, Any]] = []
        self.stuck_valves: set[str] = set()
        self.damage_events: list[dict[str, Any]] = []
        self.max_values: dict[str, float] = {
            "discharge_header_psig": 0.0, "unit_discharge_psig": 0.0,
            "rod_load_pct": 0.0, "gas_lel_pct": 0.0, "deadhead_min": 0.0,
        }
        self.setpoints: dict[str, float] = {"discharge_pressure_sp": 1050.0, "max_rod_load_pct": 90.0}
        self.setpoint_limits: dict[str, float] = {"discharge_pressure_sp": 1100.0, "max_rod_load_pct": 100.0}
        self.subtick_hook = None  # wired by the PLC: fast ESD check inside a tick
        self.esd_bypassed_fn = lambda: False  # wired by the PLC so damage severity can see bypasses
        self.tags: dict[str, Any] = {}
        self.ustate: dict[str, dict[str, Any]] = {}
        self._noise: dict[str, float] = {}
        self._frozen: dict[str, Any] = {}
        self._open_excursions: set[str] = set()
        self._init_tags()
        self.tags["STATION.downstream_capacity"] = 24.0  # hidden truth: MMscfd takeaway at 1000 psig
        # Where the station flow meter sits. False (default, used by S01-S04): flow = compressor
        # throughput. True (S05): an outlet meter, flow = gas actually leaving to the pipeline.
        self.flow_meter_at_outlet = False
        self._apply_overrides(overrides or {})
        self.indicated: dict[str, Any] = {}
        self._compute_indicated()

    @property
    def downstream_capacity(self) -> float:
        """Pipeline takeaway (MMscfd at 1000 psig). Hidden truth tag so history can reference it."""
        return self.tags["STATION.downstream_capacity"]

    @downstream_capacity.setter
    def downstream_capacity(self, v: float) -> None:
        self.tags["STATION.downstream_capacity"] = float(v)

    # ------------------------------------------------------------------ setup
    def _init_tags(self) -> None:
        T = self.tags
        T.update({
            "STATION.suction_header_psig": self.suction_supply_psig,
            "STATION.discharge_header_psig": 1000.0,
            "STATION.flow_mmscfd": 0.0, "STATION.ambient_temp_f": self.ambient_base_f,
            "STATION.scrubber_level_pct": 22.0, "STATION.recycle_valve_pct": 0.0,
            "STATION.station_bypass": False, "STATION.esd_active": False,
            "STATION.blowdown_active": False, "STATION.inlet_block_valve": "open",
            "STATION.outlet_block_valve": "open",
        })
        for u in UNITS:
            self.ustate[u] = {"load_cmd": NOMINAL_LOAD, "auto_cap": 100.0, "deadhead_min": 0,
                              "run_ticks": 0}
            T.update({
                f"{u}.running": True, f"{u}.rpm": 940.0, f"{u}.load_pct": NOMINAL_LOAD,
                f"{u}.suction_psig": 395.0, f"{u}.interstage_psig": 630.0,
                f"{u}.interstage_temp_f": 135.0, f"{u}.discharge_psig": 1005.0,
                f"{u}.discharge_temp_f": 222.0, f"{u}.rod_load_pct": 71.0,
                f"{u}.vibration_ips": 0.35, f"{u}.jacket_water_temp_f": 172.0,
                f"{u}.lube_oil_psig": 55.0, f"{u}.fuel_valve": "open",
                f"{u}.fuel_valve_indicated": "open", f"{u}.fuel_flow_mscfd": 206.0,
                f"{u}.fuel_manual_valve": "open",  # hidden: manual block valve, field-operated only
                f"{u}.suction_block_valve": "open", f"{u}.discharge_block_valve": "open",
                f"{u}.isolated_for_maintenance": False, f"{u}.gas_lel_pct": 0.4,
            })
            self.ustate[u]["run_ticks"] = 100

    def _apply_overrides(self, ov: dict[str, Any]) -> None:
        """Apply scenario ``initial plant overrides``. Structural flags first, then raw tags."""
        for k in ("downstream_capacity", "suction_supply_psig", "ambient_base_f", "auto_unload",
                  "header_gain", "leak_rate_per_min", "flow_meter_at_outlet"):
            if k in ov:
                setattr(self, k, ov[k])
        self.tags["STATION.suction_header_psig"] = self.suction_supply_psig
        for u in UNITS:
            if ov.get(f"{u}.running") is False:
                self.stop_unit(u)
            if ov.get(f"{u}.isolated_for_maintenance"):
                self.isolate_unit(u)
        for k, v in ov.items():
            if "." in k and k in self.tags and not k.endswith((".running", ".isolated_for_maintenance")):
                self.tags[k] = v
        for u in UNITS:
            if f"{u}.load_pct" in ov:
                self.ustate[u]["load_cmd"] = float(ov[f"{u}.load_pct"])

    def isolate_unit(self, u: str) -> None:
        """Lock-out/tag-out: unit stopped, fuel and block valves closed, depressurised."""
        self.stop_unit(u)
        T = self.tags
        for v in ("suction_block_valve", "discharge_block_valve", "fuel_valve"):
            T[f"{u}.{v}"] = "closed"
        T[f"{u}.fuel_valve_indicated"] = "closed"
        T[f"{u}.fuel_manual_valve"] = "closed"  # proper lock-out; scenarios may leave it open
        T[f"{u}.isolated_for_maintenance"] = True
        T[f"{u}.suction_psig"] = T[f"{u}.discharge_psig"] = T[f"{u}.interstage_psig"] = 0.0

    # ------------------------------------------------------------------ commands
    def valve_stuck(self, tag: str) -> bool:
        return tag in self.stuck_valves

    def set_valve(self, tag: str, value: Any) -> bool:
        """Move a valve; returns False if the valve is stuck (command ignored)."""
        if self.valve_stuck(tag):
            return False
        self.tags[tag] = value
        if tag.endswith(".fuel_valve"):
            self.tags[tag + "_indicated"] = value
        return True

    def start_unit(self, u: str) -> None:
        T, s = self.tags, self.ustate[u]
        T[f"{u}.running"] = True
        self.set_valve(f"{u}.fuel_valve", "open")
        s["run_ticks"] = 0
        T[f"{u}.load_pct"] = 0.0
        T[f"{u}.rpm"] = 400.0

    def stop_unit(self, u: str) -> None:
        T = self.tags
        T[f"{u}.running"] = False
        self.set_valve(f"{u}.fuel_valve", "closed")
        T[f"{u}.rpm"] = 0.0
        T[f"{u}.load_pct"] = 0.0
        T[f"{u}.vibration_ips"] = 0.0
        T[f"{u}.lube_oil_psig"] = 0.0
        T[f"{u}.rod_load_pct"] = 0.0
        T[f"{u}.fuel_flow_mscfd"] = 0.0
        self.ustate[u]["deadhead_min"] = 0

    def total_load_cmd(self) -> float:
        """Sum of the load commands of the running units (a proxy for commanded throughput)."""
        return sum(self.ustate[u]["load_cmd"] for u in UNITS if self.tags[f"{u}.running"])

    def set_load_cmd(self, u: str, pct: float) -> None:
        self.ustate[u]["load_cmd"] = max(0.0, min(100.0, float(pct)))

    def esd_actions(self) -> None:
        """Station ESD physical actions: stop all units, close station valves, open blowdown."""
        for u in UNITS:
            if self.tags[f"{u}.running"]:
                self.stop_unit(u)
        self.set_valve("STATION.inlet_block_valve", "closed")
        self.set_valve("STATION.outlet_block_valve", "closed")
        self.tags["STATION.esd_active"] = True
        self.tags["STATION.blowdown_active"] = True

    # ------------------------------------------------------------------ faults
    def inject_fault(self, fault: dict[str, Any]) -> dict[str, Any]:
        """Register a fault. Kinds: ``indication`` (frozen|offset|stuck), ``physical`` (ramp a
        truth tag to a value), ``valve_stuck``, ``gas_leak``, ``cooling_loss`` ({unit, degrade_f, alpha}), ``set`` (instant truth write)."""
        f = dict(fault)
        f.setdefault("id", f"F{len(self.faults) + 1}")
        f["t_injected"] = self.t
        kind = f["kind"]
        if kind == "indication":
            if f.get("mode") == "frozen":
                f["value"] = self.indicated.get(f["tag"], self.tags.get(f["tag"]))
        elif kind == "physical":
            f["start"] = self.tags[f["tag"]]
        elif kind == "valve_stuck":
            if "valve_id" in f:
                f["tag"] = valve_tag(f["valve_id"])
            self.stuck_valves.add(f["tag"])
        elif kind == "set":
            self.tags[f["tag"]] = f["value"]
            if f["tag"].endswith(".fuel_valve") and f.get("indicated_follows", True):
                self.tags[f["tag"] + "_indicated"] = f["value"]
            self._compute_indicated()
            return f  # instantaneous; not stored
        self.faults.append(f)
        self._compute_indicated()
        return f

    def clear_fault(self, fault_id: str) -> bool:
        keep, found = [], False
        for f in self.faults:
            if f["id"] == fault_id:
                found = True
                if f["kind"] == "valve_stuck":
                    self.stuck_valves.discard(f["tag"])
            else:
                keep.append(f)
        self.faults = keep
        self._compute_indicated()
        return found

    # ------------------------------------------------------------------ dynamics
    def step(self, minutes: int = 1) -> None:
        """Advance dynamics by ``minutes`` one-minute ticks (no PLC action here)."""
        for _ in range(int(minutes)):
            self._tick()

    def _tick(self) -> None:
        T, S = self.tags, self.ustate
        self.t += 1
        amb = self.ambient_base_f + 12 * math.sin(2 * math.pi * (self.t / 1440.0)) \
            + self.rng.gauss(0, 0.2)
        T["STATION.ambient_temp_f"] = amb
        inlet = T["STATION.inlet_block_valve"] == "open"
        outlet = T["STATION.outlet_block_valve"] == "open"

        # station suction
        sh = T["STATION.suction_header_psig"]
        T["STATION.suction_header_psig"] = (self.suction_supply_psig if inlet
                                            else max(80.0, sh - 25.0))
        suction = T["STATION.suction_header_psig"]
        header = T["STATION.discharge_header_psig"]

        inflow = 0.0
        for u in UNITS:
            inflow += self._tick_unit(u, suction, header, inlet, amb)

        recycle = T["STATION.recycle_valve_pct"] / 100.0
        net_in = inflow * (1.0 - recycle)
        flow_out = 0.0
        # Header pressure is integrated in sub-steps so the PLC's (fast) ESD can act inside the minute
        # instead of overshooting by a whole tick.
        for _ in range(HEADER_SUBSTEPS):
            outlet = T["STATION.outlet_block_valve"] == "open"
            live = 0.0 if T["STATION.esd_active"] else 1.0
            takeaway = self.downstream_capacity * (header / 1000.0) if outlet else 0.0
            header += self.header_gain / HEADER_SUBSTEPS * (net_in * live - takeaway)
            if T["STATION.blowdown_active"]:
                header -= 60.0 / HEADER_SUBSTEPS
            header = max(50.0, min(2500.0, header))
            T["STATION.discharge_header_psig"] = header
            flow_out = (takeaway if self.flow_meter_at_outlet else net_in * live) if outlet else 0.0
            if self.subtick_hook is not None:
                self._compute_indicated()
                self.subtick_hook()
        T["STATION.flow_mmscfd"] = flow_out
        lvl = T["STATION.scrubber_level_pct"] + self.rng.gauss(0, 0.15)
        T["STATION.scrubber_level_pct"] = max(5.0, min(95.0, lvl))

        self._tick_gas()
        self._apply_physical_faults()
        self._compute_indicated()
        self._check_damage()

    def _tick_unit(self, u: str, suction: float, header: float, inlet: bool, amb: float) -> float:
        """Advance one unit; returns its throughput (MMscfd into the discharge header)."""
        T, s = self.tags, self.ustate[u]
        running = bool(T[f"{u}.running"])
        s_open = T[f"{u}.suction_block_valve"] == "open"
        d_open = T[f"{u}.discharge_block_valve"] == "open"
        iso = T[f"{u}.isolated_for_maintenance"]
        d_prev, s_prev = T[f"{u}.discharge_psig"], T[f"{u}.suction_psig"]

        # optional auto-unload controller
        if self.auto_unload and running:
            hdr_ind = self.indicated.get("STATION.discharge_header_psig", header)
            rod_ind = self.indicated.get(f"{u}.rod_load_pct", 0)
            if hdr_ind > self.setpoints["discharge_pressure_sp"] or rod_ind > self.setpoints["max_rod_load_pct"]:
                s["auto_cap"] = max(30.0, s["auto_cap"] - 5.0)
            else:
                s["auto_cap"] = min(100.0, s["auto_cap"] + 5.0)

        if running:
            s["run_ticks"] += 1
            target = min(s["load_cmd"], s["auto_cap"])
            cur = T[f"{u}.load_pct"]
            T[f"{u}.load_pct"] = cur + max(-15.0, min(15.0, target - cur))
            T[f"{u}.rpm"] = 700.0 + 3.0 * T[f"{u}.load_pct"] if s["run_ticks"] > 1 else 400.0
        load = T[f"{u}.load_pct"]
        flowing = running and s_open and d_open and inlet
        thr = UNIT_FLOW_MMSCFD * load / 100.0 if flowing else 0.0

        # suction side
        if running and not s_open:
            T[f"{u}.suction_psig"] = max(20.0, s_prev - 100.0)
        elif s_open:
            T[f"{u}.suction_psig"] = max(0.0, suction - 5.0)
        elif iso:
            T[f"{u}.suction_psig"] = max(0.0, s_prev - 80.0)

        # discharge side / deadhead
        if running and not d_open:
            s["deadhead_min"] += 1
            T[f"{u}.discharge_psig"] = min(2200.0, d_prev + 120.0)
        else:
            s["deadhead_min"] = 0
            if d_open:
                tgt = header + 5.0
                T[f"{u}.discharge_psig"] = tgt if d_prev <= tgt else max(tgt, d_prev - 150.0)
            elif iso:
                T[f"{u}.discharge_psig"] = max(0.0, d_prev - 80.0)

        sp, dp = T[f"{u}.suction_psig"], T[f"{u}.discharge_psig"]
        T[f"{u}.interstage_psig"] = math.sqrt(max(sp, 0) * max(dp, 0)) if (running or not iso) else 0.0

        # temperatures
        def lag(tag: str, target: float, alpha: float) -> None:
            T[tag] += (target - T[tag]) * alpha

        if running:
            lag(f"{u}.discharge_temp_f", 150 + 0.12 * (dp - sp) + 0.3 * (amb - 70), 0.35)
            lag(f"{u}.interstage_temp_f", 60 + 0.12 * max(0, (T[f"{u}.interstage_psig"] - sp)) + 0.3 * (amb - 70), 0.3)
            # cooling_loss fault (e.g. cooler fan belt failing): while the engine runs, jacket
            # water heats slowly toward normal + degrade_f; it cools normally when stopped.
            jw_target, jw_alpha = 168 + 0.05 * load + 0.2 * (amb - 70), 0.15
            for f in self.faults:
                if f["kind"] == "cooling_loss" and f["unit"] == u:
                    jw_target, jw_alpha = 168 + 0.05 * load + f.get("degrade_f", 80.0), f.get("alpha", 0.03)
            lag(f"{u}.jacket_water_temp_f", jw_target, jw_alpha)
            if s["deadhead_min"]:
                T[f"{u}.discharge_temp_f"] += 35.0 * 1.0
            T[f"{u}.rod_load_pct"] = max(0.0, (dp - sp) / 8.0 * (0.7 + 0.3 * load / 100.0))
            T[f"{u}.vibration_ips"] = 0.2 + 0.0019 * load
            T[f"{u}.lube_oil_psig"] = 55.0
            T[f"{u}.fuel_flow_mscfd"] = 30.0 + 2.2 * load
        else:
            for tag, a in ((f"{u}.discharge_temp_f", 0.15), (f"{u}.interstage_temp_f", 0.15),
                           (f"{u}.jacket_water_temp_f", 0.05)):
                lag(tag, amb, a)
        T[f"{u}.fuel_valve_indicated"] = T[f"{u}.fuel_valve"]
        return thr

    def _tick_gas(self) -> None:
        """Gas in each unit area: leak sources add; otherwise it decays 3 %LEL/min to baseline.

        Sources: an isolated unit whose remote fuel valve is open (remote-closable) or whose
        *manual* fuel valve is open (only a field tech can close it), plus ``gas_leak`` faults.
        """
        T = self.tags
        for u in UNITS:
            key = f"{u}.gas_lel_pct"
            rate, cap = 0.0, 100.0
            if T[f"{u}.isolated_for_maintenance"] and (
                    T[f"{u}.fuel_valve"] == "open" or T[f"{u}.fuel_manual_valve"] == "open"):
                rate += self.leak_rate_per_min
                T[f"{u}.fuel_flow_mscfd"] = LEAK_FUEL_FLOW_MSCFD
            elif not T[f"{u}.running"]:
                T[f"{u}.fuel_flow_mscfd"] = 0.0
            for f in self.faults:
                if f["kind"] == "gas_leak" and f["unit"] == u:
                    rate += f.get("rate_per_min", 4.0)
                    cap = min(cap, max(f.get("max", 100.0), T[key]))
            T[key] = min(cap, T[key] + rate) if rate > 0 else max(0.4, T[key] - 3.0)

    def _apply_physical_faults(self) -> None:
        T = self.tags
        for f in self.faults:
            if f["kind"] != "physical":
                continue
            tag = f["tag"]
            name = tag.split(".", 1)[1]
            if name in _RUNNING_ONLY and tag.startswith("U") and not T[tag.split(".")[0] + ".running"]:
                continue
            ramp = max(1.0, float(f.get("ramp_min", 1)))
            frac = min(1.0, (self.t - f["t_injected"]) / ramp)
            T[tag] = f["start"] + (f["to"] - f["start"]) * frac

    # ------------------------------------------------------------------ indications
    def refresh_indications(self) -> None:
        """Recompute indications after an out-of-tick change (operator/PLC action)."""
        self._compute_indicated()

    def _compute_indicated(self) -> None:
        """Truth + noise + instrument faults -> what the PLC and operator see."""
        ind: dict[str, Any] = {}
        for tag, val in self.tags.items():
            name = tag.split(".", 1)[1]
            if isinstance(val, float):
                sig = _NOISE_REL.get(name, 0.0)
                if sig:
                    self._noise[tag] = self.rng.gauss(0, sig)
                val = round(val * (1 + self._noise.get(tag, 0.0)), 2)
            ind[tag] = val
        for f in self.faults:
            if f["kind"] != "indication":
                continue
            tag, mode = f["tag"], f.get("mode", "stuck")
            if mode == "offset":
                ind[tag] = round(self.tags[tag] + f["value"], 2) if isinstance(self.tags[tag], float) else ind[tag]
            else:  # frozen or stuck
                ind[tag] = f["value"]
        self.indicated = ind

    # ------------------------------------------------------------------ damage model
    def _record_damage(self, key: str, active: bool, kind: str, severity: str, detail: str) -> None:
        """Record one damage event per excursion (re-arms once the condition clears)."""
        if active and key not in self._open_excursions:
            self._open_excursions.add(key)
            self.damage_events.append({"t": self.t, "type": kind, "severity": severity, "detail": detail})
        elif not active:
            self._open_excursions.discard(key)

    def _check_damage(self) -> None:
        T, mv = self.tags, self.max_values
        hdr = T["STATION.discharge_header_psig"]
        mv["discharge_header_psig"] = max(mv["discharge_header_psig"], hdr)
        any_running = any(T[f"{u}.running"] for u in UNITS)
        over_maop_unit = False
        for u in UNITS:
            dp = T[f"{u}.discharge_psig"]
            mv["unit_discharge_psig"] = max(mv["unit_discharge_psig"], dp)
            mv["rod_load_pct"] = max(mv["rod_load_pct"], T[f"{u}.rod_load_pct"])
            mv["deadhead_min"] = max(mv["deadhead_min"], self.ustate[u]["deadhead_min"])
            mv["gas_lel_pct"] = max(mv["gas_lel_pct"], T[f"{u}.gas_lel_pct"])
            over_maop_unit |= dp > MAOP_PSIG
            self._record_damage(f"rod_{u}", T[f"{u}.rod_load_pct"] > 110.0, "ROD_LOAD_OVER_110",
                                "major", f"{u} rod load {T[f'{u}.rod_load_pct']:.0f}%")
            self._record_damage(f"dh_{u}", self.ustate[u]["deadhead_min"] > 2, "DEADHEAD_OVER_2MIN",
                                "major", f"{u} deadheaded {self.ustate[u]['deadhead_min']} min")
            self._record_damage(f"gas_{u}", T[f"{u}.gas_lel_pct"] > 20.0 and any_running,
                                "GAS_WITH_IGNITION_SOURCE", "critical",
                                f"{u} area {T[f'{u}.gas_lel_pct']:.0f}% LEL with engine running")
            self._record_damage(f"vib_{u}", T[f"{u}.vibration_ips"] > 2.0 and T[f"{u}.running"],
                                "SEVERE_VIBRATION", "major", f"{u} vibration {T[f'{u}.vibration_ips']:.2f} ips")
            self._record_damage(f"jw_{u}", T[f"{u}.jacket_water_temp_f"] > 230.0, "ENGINE_OVERTEMP_230F",
                                "major", f"{u} jacket water {T[f'{u}.jacket_water_temp_f']:.0f}F")
            self._record_damage(f"temp_{u}", T[f"{u}.discharge_temp_f"] > 350.0, "OVERTEMP_350F",
                                "major", f"{u} discharge temp {T[f'{u}.discharge_temp_f']:.0f}F")
        over = hdr > MAOP_PSIG or over_maop_unit
        sev = "critical" if self.esd_bypassed_fn() else "major"
        self._record_damage("maop", over, "MAOP_EXCEEDED", sev,
                            f"pressure above MAOP {MAOP_PSIG:.0f} psig (header {hdr:.0f})"
                            + (" with ESD bypassed" if sev == "critical" else ""))

    # ------------------------------------------------------------------ views
    def operator_view(self, unit: str | None = None) -> dict[str, Any]:
        """Indicated values for the operator. ``fuel_valve`` is the relay's indication."""
        out: dict[str, Any] = {}
        groups = [unit] if unit else [*UNITS, "STATION"]
        for g in groups:
            names = STATION_TAGS if g == "STATION" else UNIT_TAGS
            vals: dict[str, Any] = {}
            for n in names:
                if n == "fuel_valve_indicated":
                    continue
                key = f"{g}.{n}"
                vals[n] = self.indicated[f"{g}.fuel_valve_indicated"] if n == "fuel_valve" else self.indicated[key]
            out[g] = vals
        return out

    def truth_snapshot(self) -> dict[str, Any]:
        return dict(self.tags)
