from __future__ import annotations

import json
from pathlib import Path

from .types import Event


class JsonlLogger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: Event) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.to_json(), ensure_ascii=False) + "\n")

