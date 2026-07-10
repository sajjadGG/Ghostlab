"""Host adapter contract shared by all execution surfaces."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class HostCapabilities:
    """What a host can do — the router matches cases against these."""

    model_backed: bool = False           # a real model drives tool selection
    executes_protocol: bool = False      # can run discovery / tool_call cases
    executes_conversational: bool = False  # can run scenario cases
    executes_ui: bool = False            # can render/interact with ui:// widgets
    exposes_tool_trace: bool = False
    supports_session_resume: bool = False


@dataclass
class CaseResult:
    """Outcome of one (case, host) execution."""

    case_id: str
    suite: str
    host: str
    status: str  # pass | fail | skip | error | harness_error
    kind: str = ""  # protocol | conversational | ui
    detail: str = ""
    duration_ms: float = 0.0
    artifacts: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "case": self.case_id,
            "suite": self.suite,
            "kind": self.kind,
            "host": self.host,
            "status": self.status,
        }
        if self.detail:
            out["detail"] = self.detail
        if self.duration_ms:
            out["duration_ms"] = round(self.duration_ms, 1)
        if self.artifacts:
            out["artifacts"] = self.artifacts
        return out


class HostAdapter:
    """Base adapter. Subclasses set `capabilities` and implement `execute`."""

    id: str = "?"
    kind: str = "?"
    capabilities: HostCapabilities = HostCapabilities()

    def open(self) -> None:  # pragma: no cover - overridden where needed
        """Acquire the session/connection reused across cases."""

    def close(self) -> None:  # pragma: no cover - overridden where needed
        """Release it."""

    def can_execute(self, case: dict[str, Any]) -> Optional[str]:
        """Return None when this host can run the case, else a skip reason."""
        kind = case.get("kind", "")
        caps = self.capabilities
        if kind == "protocol" and not caps.executes_protocol:
            return f"host '{self.id}' does not execute protocol cases"
        if kind == "conversational" and not caps.executes_conversational:
            return f"host '{self.id}' does not execute conversational cases"
        if kind == "ui" and not caps.executes_ui:
            return f"host '{self.id}' does not execute UI cases"
        return None

    def execute(self, case: dict[str, Any], out_dir: Any) -> CaseResult:
        raise NotImplementedError

    def version_info(self) -> dict[str, Any]:
        """Host identity recorded in the results bundle."""
        return {"id": self.id, "kind": self.kind}
