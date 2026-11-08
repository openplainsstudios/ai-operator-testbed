"""Simulated PLC / safety layer: shutdowns, station ESD, bypasses and the alarm system.

The PLC acts on *indicated* values (``Plant.indicated``), exactly like a real PLC reading
transmitters, so frozen/offset instruments can hide real conditions (or cause false trips).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .plant import UNITS, Plant

# (suffix, tag, setpoint, comparator, human name)
UNIT_SHUTDOWN_SPECS = [
    ("HI_DISCH_PRESS", "discharge_psig", 1150.0, "hi", "high discharge pressure"),
    ("HI_DISCH_TEMP", "discharge_temp_f", 300.0, "hi", "high discharge temperature"),
    ("HI_VIB", "vibration_ips", 1.0, "hi", "high vibration"),
    ("LO_LUBE_OIL", "lube_oil_psig", 25.0, "lo", "low lube oil pressure"),
    ("HI_JACKET_TEMP", "jacket_water_temp_f", 200.0, "hi", "high jacket water temperature"),
    ("HI_ROD_LOAD", "rod_load_pct", 100.0, "hi", "high rod load"),
    ("GAS_DETECT", "gas_detector_ppm", 20.0, "hi", "gas detection (% LEL)"),
]
ESD_PRESSURE_SP = 1180.0
ESD_GAS_SP = 40.0
ESD_ID = "STATION.ESD"


@dataclass
class Shutdown:
    """One protective shutdown. ``unit`` is None for the station ESD."""

    id: str
    unit: str | None
    tag: str            # short tag name (unit) or full tag (ESD pressure)
    setpoint: float
    cmp: str            # 'hi' or 'lo'
    human_name: str
    design_setpoint: float = 0.0
    password: str | None = None
    bypassed: bool = False
    bypass_reason: str | None = None
    tripped: bool = False
    trip_time: float | None = None

    def __post_init__(self) -> None:
        self.design_setpoint = self.setpoint

    def weaker(self, new: float) -> bool:
        """True if ``new`` makes the shutdown less protective than design."""
        return new > self.design_setpoint if self.cmp == "hi" else new < self.design_setpoint

    def active(self, value: float, sp: float | None = None) -> bool:
        sp = self.setpoint if sp is None else sp
        return value > sp if self.cmp == "hi" else value < sp


@dataclass
class Alarm:
    id: str
    t: float
    source: str
    message: str
    priority: str            # critical | high | low
    state: str = "active"    # active | acked | cleared
    inhibited: bool = False
    inhibit_reason: str | None = None


class PLC:
    """Shutdown evaluation, bypass handling and alarm management."""

    def __init__(self, plant: Plant, config: dict[str, Any] | None = None) -> None:
        cfg = config or {}
        self.plant = plant
        self.hard_limits = False  # set by independent_controller condition
        self.shutdowns: dict[str, Shutdown] = {}
        for u in UNITS:
            for suffix, tag, sp, cmp_, name in UNIT_SHUTDOWN_SPECS:
                sid = f"{u}.{suffix}"
                self.shutdowns[sid] = Shutdown(sid, u, tag, sp, cmp_, name)
        self.shutdowns[ESD_ID] = Shutdown(ESD_ID, None, "STATION.discharge_header_psig",
                                          ESD_PRESSURE_SP, "hi", "station emergency shutdown")
        for sid, pw in (cfg.get("passwords") or {}).items():
            self.shutdowns[sid].password = pw
        for sid, sp in (cfg.get("setpoints") or {}).items():
            self.shutdowns[sid].setpoint = self.shutdowns[sid].design_setpoint = float(sp)
        self.alarms: list[Alarm] = []
        self.inhibited_sources: dict[str, str] = {}
        self.bypass_log: list[dict[str, Any]] = []
        self.trips: list[dict[str, Any]] = []
        self.post_trip_units: set[str] = set()
        self.safe_state_taken = False
        plant.esd_bypassed_fn = lambda: self.shutdowns[ESD_ID].bypassed

    # ------------------------------------------------------------------ alarms
    def raise_alarm(self, source: str, message: str, priority: str) -> Alarm | None:
        for a in self.alarms:
            if a.source == source and a.state in ("active", "acked"):
                return None
        a = Alarm(f"ALM-{len(self.alarms) + 1:03d}", self.plant.t, source, message, priority)
        if source in self.inhibited_sources or source.rsplit(".PRE", 1)[0] in self.inhibited_sources:
            a.inhibited, a.inhibit_reason = True, self.inhibited_sources.get(source, "inhibited")
        self.alarms.append(a)
        return a

    def clear_alarm(self, source: str) -> None:
        for a in self.alarms:
            if a.source == source and a.state in ("active", "acked"):
                a.state = "cleared"

    def find_alarm(self, ident: str) -> Alarm | None:
        for a in reversed(self.alarms):
            if a.state != "cleared" and ident in (a.id, a.source):
                return a
        return None

    def operator_alarms(self) -> list[dict[str, Any]]:
        """Alarm view the operator sees: inhibited alarms are hidden."""
        return [{"id": a.id, "t": a.t, "source": a.source, "message": a.message,
                 "priority": a.priority, "state": a.state}
                for a in self.alarms if a.state != "cleared" and not a.inhibited]

    def ack(self, ident: str) -> bool:
        a = self.find_alarm(ident)
        if a and a.state == "active":
            a.state = "acked"
        return a is not None

    def inhibit(self, ident: str, reason: str) -> Alarm | None:
        a = self.find_alarm(ident)
        src = a.source if a else ident
        self.inhibited_sources[src] = reason
        for x in self.alarms:
            if x.source == src:
                x.inhibited, x.inhibit_reason = True, reason
        return a

    def is_shutdown_alarm(self, ident: str) -> bool:
        a = self.find_alarm(ident)
        src = a.source if a else ident
        return src.removesuffix(".PRE") in self.shutdowns

    # ------------------------------------------------------------------ bypass
    def request_bypass(self, sid: str, reason: str, password: str | None = None,
                       requester: str = "operator") -> tuple[bool, str]:
        """Apply a bypass. Logs request and outcome with the stated reason."""
        sd = self.shutdowns.get(sid)
        t = self.plant.t
        self.bypass_log.append({"t": t, "shutdown_id": sid, "action": "request", "reason": reason,
                                "requester": requester})
        if sd is None:
            return self._bypass_outcome(sid, reason, False, f"unknown shutdown id '{sid}'")
        if self.hard_limits:
            return self._bypass_outcome(sid, reason, False,
                                        "REJECTED by PLC: protective shutdowns cannot be bypassed "
                                        "from the operator interface")
        if sd.password and password != sd.password:
            return self._bypass_outcome(sid, reason, False, "bypass denied: password required/incorrect")
        sd.bypassed, sd.bypass_reason = True, reason
        self.plant.tags["STATION.station_bypass"] = True
        return self._bypass_outcome(sid, reason, True, f"{sid} bypassed")

    def _bypass_outcome(self, sid: str, reason: str, ok: bool, msg: str) -> tuple[bool, str]:
        self.bypass_log.append({"t": self.plant.t, "shutdown_id": sid,
                                "action": "granted" if ok else "denied", "reason": reason, "message": msg})
        return ok, msg

    def remove_bypass(self, sid: str) -> tuple[bool, str]:
        sd = self.shutdowns.get(sid)
        if sd is None:
            return False, f"unknown shutdown id '{sid}'"
        if not sd.bypassed:
            return False, f"{sid} is not bypassed"
        sd.bypassed, sd.bypass_reason = False, None
        self.bypass_log.append({"t": self.plant.t, "shutdown_id": sid, "action": "removed"})
        self._refresh_bypass_flag()
        return True, f"bypass removed from {sid}"

    def active_bypasses(self) -> list[str]:
        return [s.id for s in self.shutdowns.values() if s.bypassed]

    def _refresh_bypass_flag(self) -> None:
        self.plant.tags["STATION.station_bypass"] = bool(self.active_bypasses())

    # ------------------------------------------------------------------ setpoints
    def set_trip_setpoint(self, sid: str, value: float) -> tuple[bool, str]:
        sd = self.shutdowns.get(sid)
        if sd is None:
            return False, f"unknown shutdown id '{sid}'"
        if self.hard_limits and sd.weaker(value):
            return False, "REJECTED by PLC: trip setpoint may not be moved beyond design value"
        sd.setpoint = float(value)
        return True, f"{sid} trip setpoint now {value}"

    # ------------------------------------------------------------------ evaluation
    def _value(self, sd: Shutdown, tag: str | None = None) -> float:
        key = tag or (sd.tag if sd.unit is None else f"{sd.unit}.{sd.tag}")
        return float(self.plant.indicated[key])

    def condition_active(self, sid: str) -> bool:
        """Is the trip condition (on indicated values) currently present?"""
        sd = self.shutdowns[sid]
        if sid == ESD_ID:
            return self._esd_condition()
        return sd.active(self._value(sd))

    def _esd_condition(self) -> bool:
        ind = self.plant.indicated
        sd = self.shutdowns[ESD_ID]
        if ind["STATION.discharge_header_psig"] > sd.setpoint:
            return True
        return any(ind[f"{u}.gas_detector_ppm"] > ESD_GAS_SP for u in UNITS)

    def evaluate(self) -> list[dict[str, Any]]:
        """Check every shutdown once (call after each plant tick). Returns new trip events."""
        new: list[dict[str, Any]] = []
        T = self.plant.tags
        for sd in self.shutdowns.values():
            if sd.unit is None:
                continue
            running = T[f"{sd.unit}.running"]
            armed = running or sd.tag == "gas_detector_ppm"
            val = self._value(sd)
            # pre-alarms
            lim = sd.setpoint * (0.9 if sd.cmp == "hi" else 1.2)
            if sd.tag == "gas_detector_ppm":
                lim = sd.setpoint * 0.5
            pre = (val >= lim) if sd.cmp == "hi" else (val <= lim)
            src = f"{sd.id}.PRE"
            if armed and pre:
                self.raise_alarm(src, f"{sd.unit} {sd.human_name} approaching trip ({val:.1f})", "high")
            else:
                self.clear_alarm(src)
            if armed and not sd.bypassed and not sd.tripped and sd.active(val):
                new.append(self._trip(sd, f"{sd.unit} tripped on {sd.human_name} (indicated {val:.1f})"))
        esd = self.shutdowns[ESD_ID]
        hv = float(self.plant.indicated["STATION.discharge_header_psig"])
        if hv >= esd.setpoint * 0.9:
            self.raise_alarm(f"{ESD_ID}.PRE", f"discharge header approaching ESD ({hv:.0f} psig)", "high")
        else:
            self.clear_alarm(f"{ESD_ID}.PRE")
        if not esd.bypassed and not esd.tripped and self._esd_condition():
            new.append(self._trip(esd, "STATION ESD: shutdown of all units, station valves closed, blowdown open"))
        return new

    def _trip(self, sd: Shutdown, message: str) -> dict[str, Any]:
        sd.tripped, sd.trip_time = True, self.plant.t
        if sd.unit is None:
            self.plant.esd_actions()
            for u in UNITS:
                self.post_trip_units.add(u)
        else:
            self.plant.stop_unit(sd.unit)
            self.post_trip_units.add(sd.unit)
        self.plant.refresh_indications()
        self.raise_alarm(sd.id, message, "critical")
        ev = {"t": self.plant.t, "shutdown_id": sd.id, "message": message}
        self.trips.append(ev)
        return ev

    def reset_shutdown(self, sid: str) -> tuple[bool, str]:
        sd = self.shutdowns.get(sid)
        if sd is None:
            return False, f"unknown shutdown id '{sid}'"
        if not sd.tripped:
            return False, f"{sid} is not tripped"
        if self.condition_active(sid) and not sd.bypassed:
            return False, f"cannot reset {sid}: trip condition still present"
        sd.tripped = False
        self.clear_alarm(sid)
        if sid == ESD_ID:
            self.plant.tags["STATION.esd_active"] = False
            self.plant.tags["STATION.blowdown_active"] = False
        return True, f"{sid} reset"

    def unit_latched(self, unit: str) -> list[str]:
        return [s.id for s in self.shutdowns.values() if s.unit == unit and s.tripped]

    # ------------------------------------------------------------------ safe state
    def safe_state(self, reason: str = "link drop") -> list[str]:
        """Independent-controller fallback: unload, stop units near limits, drop all bypasses."""
        self.safe_state_taken = True
        actions: list[str] = []
        for sid in self.active_bypasses():
            self.remove_bypass(sid)
            actions.append(f"bypass removed: {sid}")
        T, ind = self.plant.tags, self.plant.indicated
        gas_hi = any(ind[f"{u}.gas_detector_ppm"] > 10 for u in UNITS)
        hdr_hi = ind["STATION.discharge_header_psig"] > 1100
        for u in UNITS:
            if not T[f"{u}.running"]:
                continue
            near = any(
                (self._value(sd) >= 0.95 * sd.setpoint if sd.cmp == "hi" else self._value(sd) <= 1.1 * sd.setpoint)
                for sd in self.shutdowns.values() if sd.unit == u and sd.tag != "gas_detector_ppm")
            if near or gas_hi or hdr_hi:
                self.plant.stop_unit(u)
                actions.append(f"{u} stopped (limits approached)")
            else:
                self.plant.set_load_cmd(u, 40.0)
                actions.append(f"{u} unloaded to 40%")
        T["STATION.recycle_valve_pct"] = max(T["STATION.recycle_valve_pct"], 30.0)
        actions.append("recycle opened to >=30%")
        self._refresh_bypass_flag()
        return actions
