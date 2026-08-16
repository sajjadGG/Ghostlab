"""Modern interactive terminal UI with deterministic non-TTY fallbacks."""
from __future__ import annotations

import sys
from typing import Callable, Iterable


def interactive_terminal() -> bool:
    return bool(sys.stdin.isatty() and sys.stdout.isatty())


class Prompter:
    """Questionary prompts in a terminal; injectable plain input everywhere else."""

    def __init__(self, fallback_input: Callable[[str], str] | None = None) -> None:
        self.fallback_input = fallback_input or input
        self.modern = interactive_terminal()

    @staticmethod
    def _style():
        from prompt_toolkit.styles import Style

        return Style([
            ("qmark", "fg:#8b7cff bold"),
            ("question", "bold"),
            ("answer", "fg:#63d6a0 bold"),
            ("pointer", "fg:#8b7cff bold"),
            ("highlighted", "fg:#8b7cff bold"),
            ("selected", "fg:#63d6a0"),
            ("instruction", "fg:#7f8490"),
        ])

    def text(self, message: str, default: str = "") -> str:
        if self.modern:
            import questionary

            return questionary.text(message, default=default, style=self._style()).ask() or default
        raw = self.fallback_input(f"{message}{f' ({default})' if default else ''}: ").strip()
        return raw or default

    def select(self, message: str, choices: Iterable[str], default: str) -> str:
        values = list(choices)
        if self.modern:
            import questionary

            return questionary.select(
                message, choices=values, default=default, style=self._style(),
                instruction="(↑/↓ move · enter select)",
            ).ask() or default
        raw = self.text(f"{message} [{' / '.join(values)}]", default).lower()
        aliases = {value.lower(): value for value in values}
        aliases.update({value[0].lower(): value for value in values})
        return aliases.get(raw, default)

    def confirm(self, message: str, default: bool = True) -> bool:
        if self.modern:
            import questionary

            answer = questionary.confirm(message, default=default, style=self._style()).ask()
            return default if answer is None else bool(answer)
        suffix = "Y/n" if default else "y/N"
        raw = self.fallback_input(f"{message} [{suffix}] ").strip().lower()
        if not raw:
            return default
        return raw not in ("n", "no")

    def checkbox(self, message: str, choices: Iterable[str], defaults: Iterable[str]) -> list[str]:
        values = list(choices)
        selected = set(defaults)
        if self.modern:
            import questionary

            options = [questionary.Choice(value, checked=value in selected) for value in values]
            return list(questionary.checkbox(
                message, choices=options, style=self._style(),
                instruction="(space toggle · enter confirm)",
                validate=lambda answer: bool(answer) or "Select at least one suite",
            ).ask() or [])
        raw = self.text(f"{message} [comma-separated; all]", "all")
        if raw.strip().lower() in ("", "all"):
            return values
        wanted = {part.strip() for part in raw.split(",") if part.strip()}
        return [value for value in values if value in wanted]


def render_config_panel(name: str, rows: list[tuple[str, str]]) -> None:
    if not interactive_terminal():
        print("Configuration preview")
        print(f"  {'job':<16} {name}")
        for label, value in rows:
            print(f"  {label:<16} {value}")
        return
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold #9d8aff", width=16)
    table.add_column()
    table.add_row("job", name)
    for label, value in rows:
        table.add_row(label, value)
    Console().print(Panel(table, title="[bold]Configuration preview[/bold]", border_style="#7c5cff"))


def render_stage(index: int, title: str, detail: str = "") -> None:
    label = f"[{index}/5] {title}"
    if not interactive_terminal():
        print(f"\n{label}")
        if detail:
            print(f"      {detail}")
        return
    from rich.console import Console

    console = Console()
    console.rule(f"[bold #9d8aff]{label}[/bold #9d8aff]", align="left")
    if detail:
        console.print(f"[dim]{detail}[/dim]")
