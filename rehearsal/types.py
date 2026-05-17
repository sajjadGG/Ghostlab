from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class TranscriptTurn:
    role: str
    content: str


@dataclass(frozen=True)
class Event:
    type: str
    timestamp: str
    data: dict[str, Any]

    @classmethod
    def create(cls, event_type: str, **data: Any) -> "Event":
        return cls(type=event_type, timestamp=utc_now(), data=data)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

