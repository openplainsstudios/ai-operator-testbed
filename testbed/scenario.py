"""Scenario definition and loader (YAML / JSON / Python).

A scenario file is a mapping with the keys of :class:`Scenario`. Minimal YAML::

    id: tiny
    title: Downstream restriction
    description: "HIDDEN: what is really going on"
    plant_overrides: {downstream_capacity: 24}
    briefing: "Shift start. Nomination 24 MMscfd..."
    duration_min: 120
    events:
      - {at: 5, type: set_downstream_capacity, value: 10}
    ground_truth: "..."
    checks: [...]

Event types (``events[*].type``; ``at`` = sim minute):
  inject_fault        {fault: {id?, kind: indication|physical|valve_stuck|gas_leak|set, ...}}
  clear_fault         {id}
  set_downstream_capacity {value}      (MMscfd takeaway at 1000 psig)
  set_supply_pressure {value}
  set_tag             {tag, value}     (instant truth write, e.g. "U3.fuel_valve": open)
  radio_message       {text, from?}    (delivered to the operator as a notification)
  add_work_order      {id, unit?, title, status?, notes?}
  work_order_note     {wo_id, text}
  link_drop           {duration_min?}  (operator link lost; independent_controller -> safe state)

Fault kinds (see ``Plant.inject_fault``):
  {kind: indication, tag: "U1.fuel_valve_indicated", mode: stuck, value: closed}
  {kind: indication, tag: "STATION.discharge_header_psig", mode: frozen|offset, value: -80}
  {kind: physical, tag: "U2.vibration_ips", to: 1.4, ramp_min: 30}
  {kind: valve_stuck, valve_id: "U3.fuel"}
  {kind: gas_leak, unit: U3, rate_per_min: 3, max: 60}
  {kind: set, tag: "U3.fuel_valve", value: open}
  {kind: cooling_loss, unit: U2, degrade_f: 80, alpha: 0.03}   (jacket water heats while running)

Responses (``dispatch_responses`` / ``gas_control_responses``) are lists of
``{match: [keywords], default?: bool, message|reply: str, clear_faults: [ids], effects: [events]}``.

Checks / predicates: see :mod:`testbed.predicates`.
"""

from __future__ import annotations

import importlib.util
import json
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Callable

import yaml


class ScenarioError(ValueError):
    """Raised for malformed scenario files."""


@dataclass
class Scenario:
    id: str
    title: str
    description: str                      # hidden from the operator
    briefing: str                         # what the AI is told at shift start
    duration_min: int
    ground_truth: str = ""
    plant_overrides: dict[str, Any] = field(default_factory=dict)
    plc_config: dict[str, Any] = field(default_factory=dict)   # {passwords: {id: pw}, setpoints: {id: v}}
    events: list[dict[str, Any]] = field(default_factory=list)
    dispatch_responses: list[dict[str, Any]] = field(default_factory=list)
    gas_control_responses: list[dict[str, Any]] = field(default_factory=list)
    supervisor_policy: Any = field(default_factory=dict)       # dict, or callable (python scenarios)
    work_orders: list[dict[str, Any]] = field(default_factory=list)
    sops: dict[str, str] = field(default_factory=dict)
    checks: list[dict[str, Any]] = field(default_factory=list)
    correct_response: Any = None          # predicate (dict or callable) -> detection_time_min
    interventions: list[dict[str, Any]] = field(default_factory=list)  # [{id, description, predicate}]
    dispatch_necessary: bool | None = None
    tech_arrival_min: int = 90
    action_cost_min: int = 0              # sim minutes consumed by each control action
    max_turns: int = 150
    system_prompt_extra: str = ""

    def validate(self) -> None:
        for k in ("id", "title", "briefing"):
            if not getattr(self, k):
                raise ScenarioError(f"scenario missing required field '{k}'")
        if not isinstance(self.duration_min, (int, float)) or self.duration_min <= 0:
            raise ScenarioError("duration_min must be a positive number")
        for ev in self.events:
            if "at" not in ev or "type" not in ev:
                raise ScenarioError(f"event needs 'at' and 'type': {ev}")
        self.events = sorted(self.events, key=lambda e: e["at"])

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Scenario":
        known = {f.name for f in fields(cls)}
        extra = set(d) - known
        if extra:
            raise ScenarioError(f"unknown scenario keys: {sorted(extra)}")
        d = dict(d)
        d.setdefault("description", "")
        d.setdefault("briefing", "")
        d.setdefault("title", d.get("id", ""))
        try:
            s = cls(**d)
        except TypeError as e:
            raise ScenarioError(str(e)) from e
        s.validate()
        return s


def load_scenario(path: str | Path) -> Scenario:
    """Load ``.yaml``/``.yml``/``.json`` or a ``.py`` file exposing ``SCENARIO`` (Scenario or dict)
    or a ``scenario()`` factory."""
    p = Path(path)
    if not p.exists():
        raise ScenarioError(f"scenario file not found: {p}")
    if p.suffix in (".yaml", ".yml"):
        data = yaml.safe_load(p.read_text())
    elif p.suffix == ".json":
        data = json.loads(p.read_text())
    elif p.suffix == ".py":
        spec = importlib.util.spec_from_file_location(f"scenario_{p.stem}", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        data = getattr(mod, "SCENARIO", None) or mod.scenario()
        if isinstance(data, Scenario):
            data.validate()
            return data
    else:
        raise ScenarioError(f"unsupported scenario file type: {p.suffix}")
    if not isinstance(data, dict):
        raise ScenarioError(f"{p} must contain a mapping")
    return Scenario.from_dict(data)
