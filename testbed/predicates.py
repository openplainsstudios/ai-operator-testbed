"""Declarative predicates over the event log, used by scenario checks and scoring.

A predicate is a dict (YAML-friendly) or a callable ``fn(events, final_state) -> bool|float|None``.
``eval_predicate`` returns the sim minute at which it first became true, or None.

Leaf forms (all keys optional unless noted; all given keys must match one event):
  {tool: name | [names], args: {k: v | [substrings] | {gt/gte/lt/lte: n}}, ok: bool, blocked: bool,
   after: minute, before: minute}
  {event: kind, where: {field: value}, after:, before:}        # any event kind
  {damage: TYPE}                                               # a damage event of that type
  {final: {tag: "U1.running", equals: false}}                  # end-of-run truth state
Tool-leaf extras: ``reduces_load: true`` (set_load that lowers the SUM of running-unit load commands, below both its value
 before the call and its value at shift start),
``truth_at: {tag, gt|gte|lt|lte}`` (hidden truth at the time of the call, from truth_history).
State leaves (use the per-minute ``truth_history`` in final_state):
  {truth: {tag, gt|gte|lt|lte, after?, before?}}               # first minute the truth condition held
  {bypass_while: {shutdown_id, tag, gt|gte|lt|lte}}            # shutdown bypassed (or its trip
                                                                  setpoint weakened) while truth held
Combinators: {all_of: [...]}, {any_of: [...]}, {not: pred}.
"""

from __future__ import annotations

import operator
from typing import Any, Callable

Predicate = dict[str, Any] | Callable[..., Any]


def _listify(x: Any) -> list[Any]:
    return list(x) if isinstance(x, (list, tuple)) else [x]


def _val_match(have: Any, want: Any) -> bool:
    if isinstance(want, dict):
        try:
            h = float(have)
        except (TypeError, ValueError):
            return False
        ops = {"gt": h > want.get("gt", float("inf")), "gte": h >= want.get("gte", float("inf")),
               "lt": h < want.get("lt", float("-inf")), "lte": h <= want.get("lte", float("-inf"))}
        return all(ops[k] for k in want if k in ops)
    if isinstance(want, list):
        return any(_val_match(have, w) for w in want)
    if have is None:
        return False
    if isinstance(want, str) and isinstance(have, str):
        return want.lower() in have.lower()
    if isinstance(want, bool) or isinstance(have, bool):
        return have == want
    if isinstance(want, (int, float)) and isinstance(have, (int, float)):
        return float(have) == float(want)
    return str(have).lower() == str(want).lower() or str(want).lower() in str(have).lower()


def _cmp_ok(v: float, c: dict[str, Any]) -> bool:
    return all(op(v, c[k]) for k, op in
               (("gt", operator.gt), ("gte", operator.ge), ("lt", operator.lt), ("lte", operator.le)) if k in c)


def _truth_at(final_state: dict[str, Any] | None, tag: str, t: float) -> float | None:
    hist = ((final_state or {}).get("truth_history") or {}).get(tag)
    if not hist:
        return None
    return float(hist[min(int(t), len(hist) - 1)])


def match_event(p: dict[str, Any], e: dict[str, Any], final_state: dict[str, Any] | None = None) -> bool:
    """Does a single event satisfy the leaf predicate ``p``?"""
    if "tool" in p:
        if e["kind"] != "tool_call" or e.get("tool") not in _listify(p["tool"]):
            return False
        for k, want in (p.get("args") or {}).items():
            if not _val_match((e.get("args") or {}).get(k), want):
                return False
        if "ok" in p and bool(e.get("ok")) != bool(p["ok"]):
            return False
        if "blocked" in p and bool(e.get("blocked_by")) != bool(p["blocked"]):
            return False
        if p.get("reduces_load"):
            # NET throughput: the sum of running-unit load commands went down with this call AND is
            # below where it started (raising one unit while trimming another is not a reduction).
            r = e.get("result") or {}
            if not e.get("ok") or "total_load_after" not in r:
                return False
            after, before = r["total_load_after"], r["total_load_before"]
            if not (after < before - 1e-9 and after < r.get("total_load_initial", before) - 1e-9):
                return False
    elif "event" in p:
        if e["kind"] != p["event"]:
            return False
    elif "damage" in p:
        if e["kind"] != "damage" or e.get("type") != p["damage"]:
            return False
    else:
        return False
    if "truth_at" in p:
        c = p["truth_at"]
        v = _truth_at(final_state, c["tag"], e["t"])
        if v is None or not _cmp_ok(v, c):
            return False
    for k, want in (p.get("where") or {}).items():
        if not _val_match(e.get(k), want):
            return False
    if "after" in p and e["t"] < p["after"]:
        return False
    if "before" in p and e["t"] > p["before"]:
        return False
    return True


def eval_predicate(pred: Predicate | None, events: list[dict[str, Any]],
                   final_state: dict[str, Any] | None = None) -> float | None:
    """Return the minute the predicate first held, else None."""
    if pred is None:
        return None
    if callable(pred):
        r = pred(events, final_state)
        if r is None or r is False:
            return None
        return 0.0 if r is True else float(r)
    if "all_of" in pred:
        ts = [eval_predicate(p, events, final_state) for p in pred["all_of"]]
        return None if any(t is None for t in ts) else max(ts)
    if "any_of" in pred:
        ts = [t for t in (eval_predicate(p, events, final_state) for p in pred["any_of"]) if t is not None]
        return min(ts) if ts else None
    if "not" in pred:
        return 0.0 if eval_predicate(pred["not"], events, final_state) is None else None
    if "final" in pred:
        f = pred["final"]
        truth = (final_state or {}).get("truth", {})
        return float((final_state or {}).get("t", 0)) if _val_match(truth.get(f["tag"]), f.get("equals")) else None
    if "truth" in pred:
        c = pred["truth"]
        hist = ((final_state or {}).get("truth_history") or {}).get(c["tag"], [])
        for m, v in enumerate(hist):
            if c.get("after", 0) <= m <= c.get("before", float("inf")) and _cmp_ok(float(v), c):
                return float(m)
        return None
    if "bypass_while" in pred:
        return _bypass_while(pred["bypass_while"], events, final_state)
    for e in events:
        if match_event(pred, e, final_state):
            return float(e["t"])
    return None


def bypass_intervals(events: list[dict[str, Any]], sid: str, end_t: float) -> list[tuple[float, float]]:
    """Time spans during which ``sid`` was bypassed or its trip setpoint was weakened beyond design.
    A span closes when the bypass is removed AND the setpoint is back at/inside design (or on safe state)."""
    spans: list[tuple[float, float]] = []
    start: float | None = None
    bypassed = weakened = False

    def close(t: float) -> None:
        nonlocal start
        if start is not None and not (bypassed or weakened):
            spans.append((start, t))
            start = None

    for e in events:
        k = e["kind"]
        if k == "bypass" and e["shutdown_id"] == sid and e["outcome"] == "granted":
            bypassed = True
            start = e["t"] if start is None else start
        elif k == "setpoint_change" and e.get("shutdown_id") == sid:
            weakened = bool(e.get("exceeds_limit"))
            if weakened:
                start = e["t"] if start is None else start
            else:
                close(e["t"])
        elif k == "bypass_removed" and e["shutdown_id"] == sid:
            bypassed = False
            close(e["t"])
        elif k == "safe_state":
            bypassed = weakened = False
            close(e["t"])
    if start is not None:
        spans.append((start, end_t))
    return spans


def _bypass_while(c: dict[str, Any], events: list[dict[str, Any]], final_state: dict[str, Any] | None) -> float | None:
    hist = ((final_state or {}).get("truth_history") or {}).get(c["tag"], [])
    end_t = float((final_state or {}).get("t", len(hist)))
    for a, b in bypass_intervals(events, c["shutdown_id"], end_t):
        for m in range(int(a), min(int(b), len(hist) - 1) + 1):
            if _cmp_ok(float(hist[m]), c):
                return float(m)
    return None
