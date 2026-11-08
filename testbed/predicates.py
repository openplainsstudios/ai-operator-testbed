"""Declarative predicates over the event log, used by scenario checks and scoring.

A predicate is a dict (YAML-friendly) or a callable ``fn(events, final_state) -> bool|float|None``.
``eval_predicate`` returns the sim minute at which it first became true, or None.

Leaf forms (all keys optional unless noted; all given keys must match one event):
  {tool: name | [names], args: {k: v | [substrings] | {gt/gte/lt/lte: n}}, ok: bool, blocked: bool,
   after: minute, before: minute}
  {event: kind, where: {field: value}, after:, before:}        # any event kind
  {damage: TYPE}                                               # a damage event of that type
  {final: {tag: "U1.running", equals: false}}                  # end-of-run truth state
Combinators: {all_of: [...]}, {any_of: [...]}, {not: pred}.
"""

from __future__ import annotations

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


def match_event(p: dict[str, Any], e: dict[str, Any]) -> bool:
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
    elif "event" in p:
        if e["kind"] != p["event"]:
            return False
    elif "damage" in p:
        if e["kind"] != "damage" or e.get("type") != p["damage"]:
            return False
    else:
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
    for e in events:
        if match_event(pred, e):
            return float(e["t"])
    return None
