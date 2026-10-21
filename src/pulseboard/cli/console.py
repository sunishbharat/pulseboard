"""Terminal input and output for the interactive commands (testable without a terminal)."""

import getpass
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TextIO


def _stdout() -> TextIO:
    return sys.stdout


def _stderr() -> TextIO:
    return sys.stderr


@dataclass
class Console:
    """Where commands print and how they ask. Secrets are read with ``getpass`` (hidden)."""

    out: TextIO = field(default_factory=_stdout)
    err: TextIO = field(default_factory=_stderr)
    ask: Callable[[str], str] = input
    ask_secret: Callable[[str], str] = getpass.getpass

    def say(self, text: str) -> None:
        """Print a line to standard output."""
        self.out.write(text + "\n")

    def warn(self, text: str) -> None:
        """Print a line to standard error."""
        self.err.write(text + "\n")

    def prompt(self, question: str, default: str | None = None) -> str:
        """Ask a question; an empty answer takes ``default``."""
        suffix = f" [{default}]" if default else ""
        answer = self.ask(f"{question}{suffix}: ").strip()
        return answer or (default or "")

    def confirm(self, question: str, *, default: bool) -> bool:
        """Ask a yes/no question."""
        answer = self.ask(f"{question} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
        if not answer:
            return default
        return answer in {"y", "yes"}

    def secret(self, question: str) -> str:
        """Ask for a secret without echoing it."""
        return self.ask_secret(f"{question}: ").strip()
