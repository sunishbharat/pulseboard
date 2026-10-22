"""Architectural rules that tests enforce (DESIGN §4.2, §4.3).

``import-linter`` is in the DESIGN §6.5 register but not installed yet; until it is, this test
checks the one contract that has been broken before: the MCP interface never imports the engine.
"""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "pulseboard"
INTERFACES = ("mcp",)
"""Interface packages: transport, output shaping and pagination only; they go through
``PlanService``. (``cli`` and ``hosted`` also assemble the app, so they wire engine parts.)"""


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


@pytest.mark.parametrize(
    "module",
    sorted(p for package in INTERFACES for p in (SRC / package).rglob("*.py")),
    ids=lambda p: str(p.relative_to(SRC)),
)
def test_interfaces_never_import_the_engine(module: Path) -> None:
    """PB-22: business rules live behind PlanService, so a plan_id answers the same anywhere."""
    engine = sorted(
        m for m in imported_modules(module) if m.split(".")[:2] == ["pulseboard", "engine"]
    )

    assert engine == [], f"{module.name} imports {engine}; go through PlanService instead"
