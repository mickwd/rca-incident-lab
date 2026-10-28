"""Estado local de labctl: qué escenarios rompió y si están ocultos (modo desafío)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class ActiveScenario:
    name: str
    hidden: bool
    since: str


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _load(self) -> dict:
        if not self.path.exists():
            return {"active": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"active": {}}
        data.setdefault("active", {})
        return data

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def active(self) -> list[ActiveScenario]:
        items = self._load()["active"]
        return [
            ActiveScenario(name, bool(v.get("hidden")), v.get("since", ""))
            for name, v in sorted(items.items())
        ]

    def add(self, name: str, hidden: bool) -> None:
        data = self._load()
        data["active"][name] = {"hidden": hidden, "since": datetime.now(UTC).isoformat()}
        self._save(data)

    def remove(self, name: str) -> None:
        data = self._load()
        data["active"].pop(name, None)
        self._save(data)
