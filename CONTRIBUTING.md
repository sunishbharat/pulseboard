# Contributing to Pulseboard

Thank you for helping. This guide covers setup, standards and what a pull request needs.

## Setup

Pulseboard uses [`uv`](https://docs.astral.sh/uv/) for everything: environments, dependencies and running tools. No other package manager is used anywhere in the project.

```
uv sync --all-extras
uv run pre-commit install
```

## Everyday commands

| Task | Command |
|---|---|
| Tests | `uv run pytest` |
| Lint | `uv run ruff check .` |
| Format | `uv run ruff format .` |
| Type check | `uv run mypy` |
| Dependency hygiene | `uv run deptry src` |
| Vulnerability audit | `uv run pip-audit` |
| Workflow security | `uv run zizmor .github/workflows` |
| Repository hygiene | `uv run python scripts/check_hygiene.py` |

## Adding a dependency

1. Check that no existing dependency already covers the need (`docs/DESIGN_2.md` §6).
2. Add it with `uv add` (or `uv add --dev`, `uv add --optional <extra>`). Never edit dependency lists by hand.
3. Record it in `docs/DEPENDENCIES.md`: version constraint, license, why it was selected, what it replaces, maintenance status and alternatives considered.

Licenses must be permissive and compatible with the project's MIT license.

## Standards

The full standards are in `docs/DESIGN_2.md` §7. In short:

- `mypy --strict` with zero errors; Pydantic models at every boundary.
- One module per component, files under about 300 lines.
- Dependencies (sources, stores, clock, settings) are passed in; no globals.
- The engine is pure: no I/O and no clock reads.
- Every threshold or limit is a typed setting with a documented default.
- Bad Jira data produces a warning, never an exception.
- Public APIs have docstrings. No `print`, no commented-out code, no `TODO` without an issue.

## Tests

- Coverage: 85% for the engine, 70% overall.
- Unit tests make no network calls; a guard in `tests/conftest.py` fails any attempt.
- Recorded Jira cassettes must be small and scrubbed of credentials.

## Pull request checklist

- [ ] All CI gates pass locally.
- [ ] New dependencies are added with `uv` and recorded in `docs/DEPENDENCIES.md`.
- [ ] Tests cover the change.
- [ ] Docs, ADRs and the changelog are updated where behavior changed.
- [ ] No secrets in code, logs, cassettes or outputs.
- [ ] Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/).

Significant design decisions need an ADR in `docs/adr/` (use `template.md`).
