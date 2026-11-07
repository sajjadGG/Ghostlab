"""Label a batch of support tickets with an agent: one stage, eight at a time.

    ghostlab run examples/batch-label/pipeline.py --input examples/batch-label/tickets.jsonl

Interrupt it and run it again: labeled tickets are skipped, the rest resume.
A reply with a label outside the allowed set fails the item, which is retried.
"""
from __future__ import annotations

from pathlib import Path

from ghostlab import Agent, Pipeline, stage

HERE = Path(__file__).parent

PROMPT = """\
Label this support ticket with exactly one of: {labels}.

Ticket: {text}

Reply with JSON only: {{"label": "...", "confidence": 0.0-1.0, "reason": "..."}}
"""


@stage(concurrency=8, retries=2)
def label(ctx, ticket):
    labels = ctx.params["labels"]
    reply = ctx.run(ctx.agent("labeler"), PROMPT.format(labels=", ".join(labels), **ticket)).json()
    if reply.get("label") not in labels:
        raise ValueError(f"label {reply.get('label')!r} is not one of {labels}")
    return {**ticket, **reply}


pipeline = Pipeline("batch-label", [label], params={
    "labeler": Agent.load(HERE / "agents" / "labeler.yaml"),
    "labels": ["billing", "bug", "feature-request", "account", "how-to"],
})
