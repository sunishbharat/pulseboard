"""Repository hygiene check (DESIGN §7.3)."""

import codecs
from pathlib import Path

import pytest

import check_hygiene
from check_hygiene import (
    ATTRIBUTION_RULE,
    PACKAGE_MANAGER_RULE,
    scan_files,
    scan_text,
)

# Built at run time so this test file does not contain the terms itself.
TOOL_NAME = codecs.decode("Pynhqr", "rot13")


@pytest.mark.parametrize(
    "line",
    [
        f"Co-Authored-By: {TOOL_NAME} <noreply@example.com>",
        f"Written with {TOOL_NAME.lower()}",
        f"see .{TOOL_NAME.lower()}/settings.json",
        codecs.decode("Guvf svyr vf NV-trarengrq.", "rot13"),
    ],
)
def test_attribution_found(line: str) -> None:
    assert [f.rule for f in scan_text("x", line)] == [ATTRIBUTION_RULE]


@pytest.mark.parametrize(
    "line",
    [
        "pip install requests",
        "python -m pip install -U x",
        "uv pip install x",
        "poetry add httpx",
        "conda install numpy",
    ],
)
def test_other_package_managers_found(line: str) -> None:
    assert [f.rule for f in scan_text("x", line)] == [PACKAGE_MANAGER_RULE]


@pytest.mark.parametrize(
    "line",
    [
        "uv add fastmcp",
        "uv run pip-audit",
        "Serves AI clients over MCP.",
        "the openapi schema",
        "Co-Authored-By: A Person <a@example.com>",
    ],
)
def test_clean_lines(line: str) -> None:
    assert scan_text("x", line) == []


def test_allowlist_and_locations() -> None:
    text = "ok\npip install x\n"

    assert scan_text("f", text)[0].location == "f:2"
    assert scan_text("f", text, frozenset({PACKAGE_MANAGER_RULE})) == []


def test_scan_files_skips_self_and_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "check_hygiene.py").write_text("pip install x")
    (tmp_path / "blob.bin").write_bytes(b"\xff\xfe\x00pip install")
    (tmp_path / "README.md").write_text("pip install x")

    findings = scan_files(["scripts/check_hygiene.py", "blob.bin", "README.md", "missing.txt"])

    assert [f.location for f in findings] == ["README.md:1"]


def test_main_exit_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "clean.md").write_text("uv add x")
    (tmp_path / "dirty.md").write_text("pip install x")

    assert check_hygiene.main(["clean.md"]) == 0
    assert check_hygiene.main(["clean.md", "dirty.md"]) == 1
