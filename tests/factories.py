"""Builders for domain objects used across tests."""

from datetime import UTC, date, datetime

from pulseboard.domain.snapshot import (
    BoardColumn,
    BoardSnapshot,
    ExternalBlocker,
    Person,
    Sprint,
    StatusInfo,
    StatusTransition,
    Task,
    Version,
)
from pulseboard.domain.types import (
    AccountId,
    BoardId,
    BoardType,
    EstimationMode,
    IssueId,
    IssueKey,
    SiteId,
    StatusCategory,
)
from pulseboard.domain.untrusted import UntrustedText

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
PLAN_DATE = date(2026, 10, 19)  # a Monday
"""Default plan date for engine tests."""

STATUS_IDS = {StatusCategory.TO_DO: "1", StatusCategory.IN_PROGRESS: "3", StatusCategory.DONE: "5"}
STATUSES = (
    StatusInfo(status_id="1", name=UntrustedText("To Do"), category=StatusCategory.TO_DO),
    StatusInfo(
        status_id="3", name=UntrustedText("In Progress"), category=StatusCategory.IN_PROGRESS
    ),
    StatusInfo(status_id="5", name=UntrustedText("Done"), category=StatusCategory.DONE),
)
COLUMNS = (
    BoardColumn(name=UntrustedText("To Do"), status_ids=("1",)),
    BoardColumn(name=UntrustedText("In Progress"), status_ids=("3",)),
    BoardColumn(name=UntrustedText("Done"), status_ids=("5",)),
)


def task(
    issue_id: int, *, category: StatusCategory = StatusCategory.TO_DO, **changes: object
) -> Task:
    """An issue on the default board, assigned to alice, ranked by ID; ``changes`` override.

    The raw fields that board-wide fields are derived from follow the given ones, as the Jira
    normalizer would set them: ``rank_value`` from ``board_rank``, and ``link_blockers`` from
    ``blocked_by`` and ``external_blockers`` (so ``link_board`` and merges keep them).
    """
    key = f"ABC-{issue_id}"
    fields: dict[str, object] = {
        "issue_id": IssueId(issue_id),
        "issue_key": IssueKey(key),
        "summary": UntrustedText(f"Task {issue_id}"),
        "url": f"https://acme.atlassian.net/browse/{key}",
        "issue_type": UntrustedText("Story"),
        "status_id": STATUS_IDS[category],
        "status": UntrustedText(category.value),
        "status_category": category,
        "assignee": AccountId("alice"),
        "board_rank": issue_id,
        "created": T0,
    }
    fields.update(changes)
    rank = fields["board_rank"]
    fields.setdefault("rank_value", f"{rank:010d}" if isinstance(rank, int) else "")
    if "link_blockers" not in changes:
        blocked = changes.get("blocked_by", ())
        external = changes.get("external_blockers", ())
        assert isinstance(blocked, tuple)
        assert isinstance(external, tuple)
        fields["link_blockers"] = (
            *(
                ExternalBlocker(
                    issue_id=IssueId(b),
                    issue_key=IssueKey(f"ABC-{b}"),
                    status_category=StatusCategory.TO_DO,
                )
                for b in blocked
            ),
            *external,
        )
    return Task.model_validate(fields)


def person(account_id: str, *, capacity: float = 1.0, country: str = "US") -> Person:
    """A person who can be assigned work."""
    return Person(
        account_id=AccountId(account_id),
        display_name=UntrustedText(account_id.title()),
        capacity=capacity,
        holiday_country=country,
    )


def snapshot(
    tasks: tuple[Task, ...] = (),
    *,
    board_type: BoardType = BoardType.SCRUM,
    sprints: tuple[Sprint, ...] = (),
    versions: tuple[Version, ...] = (),
    transitions: tuple[StatusTransition, ...] = (),
    people: tuple[Person, ...] = (),
    estimation_mode: EstimationMode = EstimationMode.STORY_POINTS,
) -> BoardSnapshot:
    """A board snapshot with the default columns and statuses. Its tasks' dependencies are
    "Blocks" links, as on a Jira board with the default settings."""
    return BoardSnapshot(
        site_id=SiteId("acme"),
        board_id=BoardId(42),
        account_id=AccountId("acc-1"),
        board_name=UntrustedText("Team board"),
        board_type=board_type,
        columns=COLUMNS,
        statuses=STATUSES,
        estimation_mode=estimation_mode,
        people=people,
        sprints=sprints,
        versions=versions,
        tasks=tasks,
        transitions=transitions,
        fetched_at=T0,
        link_types=("Blocks",) if any(t.link_blockers for t in tasks) else (),
    )


def fixture_board() -> BoardSnapshot:
    """A small but realistic Scrum board: 3 people, an active and a future sprint, dependencies,
    in-progress, unassigned, unestimated and done work, a milestone and status history."""
    from pulseboard.domain.types import SprintId, SprintState, VersionId

    active = Sprint(
        sprint_id=SprintId(11),
        name=UntrustedText("Sprint 11"),
        state=SprintState.ACTIVE,
        start=datetime(2026, 10, 12, 9, tzinfo=UTC),
        end=datetime(2026, 10, 23, 17, tzinfo=UTC),
    )
    future = Sprint(
        sprint_id=SprintId(12),
        name=UntrustedText("Sprint 12"),
        state=SprintState.FUTURE,
        start=datetime(2026, 10, 26, 9, tzinfo=UTC),
        end=datetime(2026, 11, 6, 17, tzinfo=UTC),
    )
    closed = Sprint(
        sprint_id=SprintId(10),
        name=UntrustedText("Sprint 10"),
        state=SprintState.CLOSED,
        start=datetime(2026, 9, 28, 9, tzinfo=UTC),
        end=datetime(2026, 10, 9, 17, tzinfo=UTC),
    )
    release = Version(
        version_id=VersionId(300),
        name=UntrustedText("1.0"),
        release_date=date(2026, 10, 30),
    )
    s11, s12, s10 = SprintId(11), SprintId(12), SprintId(10)
    v = (VersionId(300),)
    done = StatusCategory.DONE
    doing = StatusCategory.IN_PROGRESS
    tasks = (
        task(
            101,
            category=done,
            sprint_id=s10,
            story_points=8.0,
            resolved=datetime(2026, 10, 2, 15, tzinfo=UTC),
        ),
        task(
            102,
            category=done,
            sprint_id=s10,
            story_points=13.0,
            assignee="bob",
            resolved=datetime(2026, 10, 8, 15, tzinfo=UTC),
        ),
        task(
            103,
            category=done,
            sprint_id=s11,
            story_points=2.0,
            assignee="carol",
            resolved=datetime(2026, 10, 15, 11, tzinfo=UTC),
        ),
        task(
            104,
            category=doing,
            sprint_id=s11,
            story_points=5.0,
            remaining_estimate_hours=8.0,
            fix_version_ids=v,
        ),
        task(
            105, category=doing, sprint_id=s11, story_points=3.0, assignee="bob", fix_version_ids=v
        ),
        task(106, sprint_id=s11, story_points=2.0, blocked_by=(IssueId(104),), fix_version_ids=v),
        task(
            107,
            sprint_id=s11,
            story_points=3.0,
            assignee="carol",
            blocked_by=(IssueId(105),),
            fix_version_ids=v,
        ),
        task(108, sprint_id=s11, assignee="carol", fix_version_ids=v),
        task(
            109,
            sprint_id=s12,
            story_points=5.0,
            blocked_by=(IssueId(106), IssueId(107)),
            fix_version_ids=v,
            due_date=date(2026, 10, 30),
        ),
        task(110, sprint_id=s12, story_points=3.0, assignee="bob"),
        task(111, sprint_id=s12, story_points=2.0, assignee=None),
        task(112, sprint_id=s12, story_points=8.0, assignee="carol", blocked_by=(IssueId(109),)),
    )
    return snapshot(
        tasks,
        sprints=(closed, active, future),
        versions=(release,),
        people=(person("alice"), person("bob"), person("carol")),
    )


FLOW_PLAN_DATE = date(2026, 10, 17)
"""Plan date for ``flow_board`` (hand-computed flow metrics)."""


def at(day: int, hour: int) -> datetime:
    return datetime(2026, 10, day, hour, tzinfo=UTC)


def move(issue: int, entry: int, src: str, dst: str, when: datetime) -> StatusTransition:
    return StatusTransition(
        issue_id=IssueId(issue), entry_id=entry, from_status_id=src, to_status_id=dst, at=when
    )


def flow_board() -> BoardSnapshot:
    """
    1: To Do → In Progress (12th 10:00) → Done (15th 16:00): cycle time 3.25 days.
    2: To Do → In Progress (14th 09:00), still in progress: age 3 days.
    3: created on the 16th, still To Do.
    4: To Do → In Progress (10th 08:00), still in progress: age 7 days (an outlier).
    """
    return snapshot(
        (
            task(1, category=StatusCategory.DONE, created=at(10, 9), resolved=at(15, 16)),
            task(2, category=StatusCategory.IN_PROGRESS, created=at(10, 9)),
            task(3, created=at(16, 9)),
            task(4, category=StatusCategory.IN_PROGRESS, created=at(1, 9)),
        ),
        transitions=(
            move(1, 11, "1", "3", at(12, 10)),
            move(1, 12, "3", "5", at(15, 16)),
            move(2, 21, "1", "3", at(14, 9)),
            move(4, 41, "1", "3", at(10, 8)),
        ),
    )
