"""Host adapters: the agents/protocol surfaces that execute test-plan cases.

A *host* is whatever sits between GhostLab and the MCP under test: the direct
protocol client (no model), a Codex session, a process-spawned agent, or —
later — browser-driven hosts. Adapters declare capabilities so the test
runner can route each case to hosts that can actually execute it (roadmap
Phase A4).
"""
from typing import Any

from .base import CaseResult, HostAdapter, HostCapabilities
from .direct import DirectMcpHost
from .runner import RunnerHost

__all__ = [
    "CaseResult",
    "HostAdapter",
    "HostCapabilities",
    "DirectMcpHost",
    "RunnerHost",
    "build_hosts",
]


def build_hosts(
    spec,
    spec_path,
    timeout: float = 30.0,
    *,
    backend: Any | None = None,
    show_progress: bool = True,
    user_runner_config: Any | None = None,
) -> list[HostAdapter]:
    """Instantiate adapters for a spec's `hosts` section.

    Unknown kinds are skipped (reported by the caller via the returned list's
    ids); a spec with no hosts still gets the implicit direct-mcp host so the
    protocol suites are always executable. ``backend`` (a CodexBackend) lets
    RunnerHost score conversational runs with a judge instead of only
    "did it finish"; omit it to run conversational cases without judging.
    ``user_runner_config`` is the *shared* user-emulator runner used by every
    RunnerHost — it must never be the same config as a host's `config_ref`
    (which wires the target MCP into the agent-under-test), since the user
    emulator plays a human, not another tool-using agent.
    """
    target = spec.target_config()
    adapters: list[HostAdapter] = []
    for host in spec.hosts or []:
        kind = str(host.get("kind", ""))
        host_id = str(host.get("id", kind or "?"))
        if kind == "direct-mcp":
            adapters.append(DirectMcpHost(host_id, target, timeout=timeout))
        elif kind in ("process", "codex-session"):
            adapters.append(
                RunnerHost(host_id, kind, host, spec_path, backend=backend,
                          show_progress=show_progress,
                          user_runner_config=user_runner_config)
            )
    if not any(isinstance(adapter, DirectMcpHost) for adapter in adapters):
        adapters.insert(0, DirectMcpHost("direct-mcp", target, timeout=timeout))
    return adapters
