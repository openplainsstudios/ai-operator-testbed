"""Append-only event log with simulated time stamps."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class EventLog:
    """Ordered list of event dicts. Every entry has ``seq``, ``t`` (sim minute) and ``kind``."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def add(self, t: float, kind: str, **data: Any) -> dict[str, Any]:
        """Append an event and return it."""
        ev = {"seq": len(self.events), "t": t, "kind": kind, **data}
        self.events.append(ev)
        return ev

    def of_kind(self, kind: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e["kind"] == kind]

    def write_jsonl(self, path: Path) -> None:
        with open(path, "w") as f:
            for e in self.events:
                f.write(json.dumps(e, default=str) + "\n")
