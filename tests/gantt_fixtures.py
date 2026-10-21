"""The four Gantt visual review fixtures of Addendum 02 §10 (also rendered by
``scripts/render_gantt_review.py``)."""

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

from factories import STATUSES, T0, fixture_board, person, snapshot, task
from pulseboard.domain.snapshot import BoardSnapshot, Epic, Sprint, Task
from pulseboard.domain.types import (
    AccountId,
    BoardType,
    IssueId,
    IssueKey,
    SprintId,
    SprintState,
    StatusCategory,
)
from pulseboard.domain.untrusted import UntrustedText
from pulseboard.reports.base import ChartOptions

PAYMENTS, SEARCH = IssueId(90), IssueId(91)
SCRUM_EPICS = {
    PAYMENTS: (101, 104, 106, 109, 111, 112),
    SEARCH: (102, 103, 105, 107, 108, 110),
}
SUMMARIES = {
    104: "Validate card input",
    105: "Index product catalogue",
    106: "Retry policy for declined payments",
    107: "Search ranking by popularity",
    108: "Search result page empty state",
    109: "Payment API retries and idempotency keys",
    110: "Synonym dictionary import",
    111: "Refund e-mail template",
    112: "Settlement report export for finance",
}


STATUS_NAMES = {s.category: s.name for s in STATUSES}
"""Jira status names ("To Do"), as on a real board."""


def _named(tasks: tuple[Task, ...]) -> tuple[Task, ...]:
    return tuple(t.model_copy(update={"status": STATUS_NAMES[t.status_category]}) for t in tasks)


def _epic(issue_id: IssueId, name: str) -> Epic:
    return Epic(issue_id=issue_id, issue_key=IssueKey(f"ABC-{issue_id}"), name=UntrustedText(name))


def scrum_board() -> BoardSnapshot:
    """Small Scrum board: 9 open tasks, 3 people, 2 epics, 1 milestone (the fixture board)."""
    board = fixture_board()
    epic_of = {key: epic for epic, keys in SCRUM_EPICS.items() for key in keys}
    tasks = tuple(
        t.model_copy(
            update={
                "epic_issue_id": epic_of[int(t.issue_id)],
                "summary": UntrustedText(SUMMARIES.get(int(t.issue_id), str(t.summary))),
            }
        )
        for t in board.tasks
    )
    epics = (_epic(PAYMENTS, "Payments"), _epic(SEARCH, "Search"))
    return board.model_copy(update={"tasks": _named(tasks), "epics": epics})


def kanban_board() -> BoardSnapshot:
    """Kanban board with low estimates: Monte Carlo is the primary forecast."""
    created = datetime(2026, 8, 3, 9, tzinfo=UTC)
    people = ("dana", "eli", "fay")
    done = tuple(
        task(
            n,
            category=StatusCategory.DONE,
            created=created,
            resolved=created + timedelta(days=4 * n),
            assignee=AccountId(people[n % 3]),
        )
        for n in range(1, 18)
    )
    open_work = tuple(
        task(
            n,
            created=created,
            assignee=AccountId(people[n % 3]) if n % 5 else None,
            story_points=3.0 if n % 4 == 0 else None,
            blocked_by=(IssueId(n - 1),) if n % 3 == 0 and n > 20 else (),
            category=StatusCategory.IN_PROGRESS if n < 23 else StatusCategory.TO_DO,
            summary=UntrustedText(f"Support request {n}"),
        )
        for n in range(20, 34)
    )
    return snapshot(
        _named((*done, *open_work)), board_type=BoardType.KANBAN, people=tuple(map(person, people))
    )


LARGE_EPICS = (
    "Accounts",
    "Billing",
    "Catalogue",
    "Checkout",
    "Data platform",
    "Mobile app",
    "Notifications",
    "Search",
)


def large_board() -> BoardSnapshot:
    """20 people, 200 open tasks in 8 epics, with dependency chains inside each epic."""
    people = tuple(f"dev{n:02d}" for n in range(1, 21))
    epics = tuple(_epic(IssueId(900 + i), name) for i, name in enumerate(LARGE_EPICS))
    sprint = Sprint(
        sprint_id=SprintId(30),
        name=UntrustedText("Sprint 30"),
        state=SprintState.ACTIVE,
        start=datetime(2026, 10, 12, 9, tzinfo=UTC),
        end=datetime(2026, 10, 23, 17, tzinfo=UTC),
    )
    tasks: list[Task] = []
    for n in range(200):
        issue = 1000 + n
        epic = epics[n % len(epics)]
        tasks.append(
            task(
                issue,
                created=T0,
                summary=UntrustedText(f"{epic.name} story {n // len(epics) + 1}"),
                assignee=AccountId(people[n % len(people)]),
                story_points=float((1, 2, 3, 5, 8)[n % 5]),
                epic_issue_id=epic.issue_id,
                blocked_by=(IssueId(issue - len(epics)),) if n >= len(epics) and n % 3 else (),
                sprint_id=SprintId(30) if n < 40 else None,
            )
        )
    return snapshot(
        _named(tuple(tasks)), people=tuple(map(person, people)), sprints=(sprint,)
    ).model_copy(update={"epics": epics})


FILTERED_VIEW = ChartOptions(assignees=(AccountId("carol"),))
"""Carol's work only: ABC-107 waits for Bob's ABC-105, which is filtered out."""

REVIEW_FIXTURES: dict[str, tuple[Callable[[], BoardSnapshot], ChartOptions]] = {
    "1-scrum-small": (scrum_board, ChartOptions()),
    "2-kanban-low-estimates": (kanban_board, ChartOptions()),
    "3-large-20-people-200-tasks": (large_board, ChartOptions()),
    "4-filtered-hidden-predecessor": (scrum_board, FILTERED_VIEW),
}
REVIEW_PLAN_DATE = date(2026, 10, 19)
