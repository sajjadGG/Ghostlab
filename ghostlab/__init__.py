"""Ghostlab: a headless runner for coding agents, and an SDK for composing
checkpointed, resumable jobs out of them.

```python
from ghostlab import Agent, Pipeline, stage

reviewer = Agent.load("agents/reviewer.yaml")

@stage(concurrency=4)
def review(ctx, item):
    return ctx.run(reviewer, f"Review {item['path']} and reply with JSON").json()

Pipeline("reviews", [review]).run([{"id": "a", "path": "src/a.py"}], run_dir="runs/reviews")
```
"""

from .agent import MCP, Agent, ConfigError
from .pipeline import Context, Pipeline, RunReport, Stage, stage
from .sandbox import Sandbox, SandboxError
from .session import AgentError, Result, Session

__version__ = "0.5.0"

__all__ = [
    "MCP", "Agent", "AgentError", "ConfigError", "Context", "Pipeline", "Result",
    "RunReport", "Sandbox", "SandboxError", "Session", "Stage", "stage", "__version__",
]
