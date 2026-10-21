"""Jira data → ``BoardSnapshot`` (DESIGN §4.2 Normalizer). Pure: no I/O.

- Identity is the issue ID and the account ID (Data Center: user key), never key or name.
- Link direction: an issue with an inward link of a dependency type (default ``Blocks``,
  "is blocked by") is blocked by the linked issue. Linked issues not fetched with the board
  become external blockers.
- Subtasks: dropped when ``include_subtasks`` is off; otherwise scheduled as tasks, and their
  parent is blocked by them (the parent finishes after its subtasks).
- Changelog → status transitions and sprint-membership changes, deduplicated by entry ID.
- Unparseable estimates become ``None`` with ``invalid_estimate`` set (warning W004).
"""

from collections.abc import Mapping
from datetime import date, datetime

from pydantic import JsonValue

from pulseboard.domain.snapshot import (
    BoardColumn,
    BoardSnapshot,
    Epic,
    ExternalBlocker,
    Person,
    Sprint,
    SprintMembershipChange,
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
    Hours,
    IssueId,
    IssueKey,
    SiteId,
    SprintId,
    SprintState,
    StatusCategory,
    VersionId,
)
from pulseboard.domain.untrusted import UntrustedText
from pulseboard.settings import TeamSettings
from pulseboard.sources.jira.models import (
    JiraBoardData,
    JiraHistory,
    JiraIssue,
    JiraStatus,
)

_CATEGORY = {
    "new": StatusCategory.TO_DO,
    "indeterminate": StatusCategory.IN_PROGRESS,
    "done": StatusCategory.DONE,
}
_SPRINT_STATE = {
    "future": SprintState.FUTURE,
    "active": SprintState.ACTIVE,
    "closed": SprintState.CLOSED,
}
_TIME_ESTIMATE_FIELD = "timeoriginalestimate"
_RANK_SCHEMA = "com.pyxis.greenhopper.jira:gh-lexo-rank"
_EPIC_TYPE = "epic"
_SECONDS_PER_HOUR = 3600


def category_of(status: JiraStatus) -> StatusCategory:
    """Map a Jira status category key; unknown keys count as "to do"."""
    return _CATEGORY.get(status.status_category.key, StatusCategory.TO_DO)


def coerce_points(value: JsonValue) -> tuple[float | None, bool]:
    """Story points from a loosely typed field: (points, invalid)."""
    if value is None:
        return None, False
    if isinstance(value, bool):
        return None, True
    if isinstance(value, int | float):
        return (float(value), False) if value >= 0 else (None, True)
    if isinstance(value, str):
        try:
            points = float(value.strip())
        except ValueError:
            return None, True
        return (points, False) if points >= 0 else (None, True)
    return None, True


def _estimation(data: JiraBoardData) -> tuple[EstimationMode, str | None]:
    estimation = data.configuration.estimation
    if estimation is None or estimation.field is None or estimation.type != "field":
        return EstimationMode.NONE, None
    field_id = estimation.field.field_id
    if field_id == _TIME_ESTIMATE_FIELD:
        return EstimationMode.TIME, field_id
    return EstimationMode.STORY_POINTS, field_id


def _sprint(raw_id: int, data: JiraBoardData) -> Sprint | None:
    for s in data.sprints:
        if s.id == raw_id:
            return Sprint(
                sprint_id=SprintId(s.id),
                name=UntrustedText(s.name),
                state=_SPRINT_STATE.get(s.state, SprintState.FUTURE),
                start=s.start_date,
                end=s.end_date,
                goal=UntrustedText(s.goal) if s.goal else None,
            )
    return None


def _rank_order(data: JiraBoardData, issues: list[JiraIssue]) -> dict[str, int]:
    rank_field = next(
        (f.id for f in data.fields if f.schema_ and f.schema_.custom == _RANK_SCHEMA), None
    )

    def key(position: int, issue: JiraIssue) -> tuple[str, int]:
        value = issue.fields.custom_value(rank_field) if rank_field else None
        return (value if isinstance(value, str) else "", position)

    ordered = sorted(enumerate(issues), key=lambda pair: key(*pair))
    return {issue.id: rank for rank, (_, issue) in enumerate(ordered)}


def _epic_id(issue: JiraIssue) -> IssueId | None:
    if issue.fields.epic is not None:
        return IssueId(issue.fields.epic.id)
    parent = issue.fields.parent
    if parent and parent.fields.issuetype and parent.fields.issuetype.name.lower() == _EPIC_TYPE:
        return IssueId(int(parent.id))
    return None


def _date_of(value: date | datetime | None) -> date | None:
    return value.date() if isinstance(value, datetime) else value


def _hours(seconds: int | None) -> Hours | None:
    return None if seconds is None else Hours(seconds / _SECONDS_PER_HOUR)


def _changes(
    issue_id: IssueId, histories: tuple[JiraHistory, ...]
) -> tuple[list[StatusTransition], list[SprintMembershipChange]]:
    transitions: list[StatusTransition] = []
    sprint_changes: list[SprintMembershipChange] = []
    for history in {h.id: h for h in histories}.values():
        entry = int(history.id)
        for item in history.items:
            if item.field == "status" and item.to_value:
                transitions.append(
                    StatusTransition(
                        issue_id=issue_id,
                        entry_id=entry,
                        from_status_id=item.from_value,
                        to_status_id=item.to_value,
                        at=history.created,
                    )
                )
            elif item.field == "Sprint":
                before = {int(x) for x in (item.from_value or "").split(",") if x.strip()}
                after = {int(x) for x in (item.to_value or "").split(",") if x.strip()}
                sprint_changes += [
                    SprintMembershipChange(
                        issue_id=issue_id,
                        entry_id=entry,
                        sprint_id=SprintId(s),
                        added=s in after,
                        at=history.created,
                    )
                    for s in sorted(before ^ after)
                ]
    return transitions, sprint_changes


def normalize(
    data: JiraBoardData,
    *,
    site_id: SiteId,
    base_url: str,
    team: TeamSettings,
    fetched_at: datetime,
) -> BoardSnapshot:
    """Build the snapshot for one board."""
    mode, estimation_field = _estimation(data)
    issues = [i for i in data.issues if team.include_subtasks or not i.fields.issuetype.subtask]
    on_board = {i.id for i in issues}
    rank = _rank_order(data, issues)
    link_types = set(team.dependency_link_types)
    subtasks_of: dict[str, list[IssueId]] = {}
    for issue in issues:
        parent = issue.fields.parent
        if issue.fields.issuetype.subtask and parent and parent.id in on_board:
            subtasks_of.setdefault(parent.id, []).append(IssueId(int(issue.id)))

    configured = {p.account_id: p for p in team.people}
    people: dict[AccountId, Person] = {}
    tasks: list[Task] = []
    transitions: list[StatusTransition] = []
    sprint_changes: list[SprintMembershipChange] = []
    for issue in issues:
        f = issue.fields
        issue_id = IssueId(int(issue.id))
        assignee = AccountId(f.assignee.identity) if f.assignee and f.assignee.identity else None
        if assignee and assignee not in people:
            settings = configured.get(assignee)
            people[assignee] = Person(
                account_id=assignee,
                display_name=UntrustedText(f.assignee.display_name or assignee)
                if f.assignee
                else UntrustedText(assignee),
                capacity=(
                    settings.capacity if settings and settings.capacity else team.default_capacity
                ),
                holiday_country=(
                    settings.holiday_country
                    if settings and settings.holiday_country
                    else team.default_holiday_country
                ),
            )
        blocked_by: list[IssueId] = list(subtasks_of.get(issue.id, []))
        external: list[ExternalBlocker] = []
        for link in f.issuelinks:
            other = link.inward_issue
            if link.type.name not in link_types or other is None:
                continue
            if other.id in on_board:
                blocked_by.append(IssueId(int(other.id)))
            else:
                status = other.fields.status
                external.append(
                    ExternalBlocker(
                        issue_id=IssueId(int(other.id)),
                        issue_key=IssueKey(other.key),
                        status_category=category_of(status) if status else StatusCategory.TO_DO,
                    )
                )
        points, invalid = (
            coerce_points(f.custom_value(estimation_field))
            if mode is EstimationMode.STORY_POINTS and estimation_field
            else (None, False)
        )
        tasks.append(
            Task(
                issue_id=issue_id,
                issue_key=IssueKey(issue.key),
                summary=UntrustedText(f.summary or ""),
                url=f"{base_url.rstrip('/')}/browse/{issue.key}",
                issue_type=UntrustedText(f.issuetype.name),
                status_id=f.status.id,
                status=UntrustedText(f.status.name),
                status_category=category_of(f.status),
                assignee=assignee,
                board_rank=rank[issue.id],
                priority=UntrustedText(f.priority.name) if f.priority else None,
                story_points=points,
                invalid_estimate=invalid,
                original_estimate_hours=_hours(f.timeoriginalestimate),
                remaining_estimate_hours=_hours(f.timeestimate),
                due_date=f.duedate,
                created=f.created,
                resolved=f.resolutiondate,
                sprint_id=SprintId(f.sprint.id) if f.sprint else None,
                epic_issue_id=_epic_id(issue),
                fix_version_ids=tuple(VersionId(int(v.id)) for v in f.fix_versions),
                blocked_by=tuple(blocked_by),
                external_blockers=tuple(external),
            )
        )
        found_transitions, found_sprint_changes = _changes(
            issue_id, data.histories.get(issue.id, ())
        )
        transitions += found_transitions
        sprint_changes += found_sprint_changes

    due_dates: Mapping[str, date | None] = {e.id: e.fields.duedate for e in data.epic_due_dates}
    sprints = [s for s in (_sprint(raw.id, data) for raw in data.sprints) if s is not None]
    board_type = BoardType.SCRUM if data.board.type == "scrum" else BoardType.KANBAN
    return BoardSnapshot(
        site_id=site_id,
        board_id=BoardId(data.board.id),
        account_id=AccountId(data.myself.identity or "unknown"),
        board_name=UntrustedText(data.board.name),
        board_type=board_type,
        columns=tuple(
            BoardColumn(name=UntrustedText(c.name), status_ids=tuple(s.id for s in c.statuses))
            for c in data.configuration.column_config.columns
        ),
        statuses=tuple(
            StatusInfo(status_id=s.id, name=UntrustedText(s.name), category=category_of(s))
            for s in data.statuses
        ),
        estimation_mode=mode,
        estimation_field=estimation_field,
        people=tuple(people.values()),
        sprints=tuple(sprints),
        versions=tuple(
            Version(
                version_id=VersionId(int(v.id)),
                name=UntrustedText(v.name),
                release_date=_date_of(v.release_date),
                released=v.released,
            )
            for v in data.versions
        ),
        epics=tuple(
            Epic(
                issue_id=IssueId(e.id),
                issue_key=IssueKey(e.key),
                name=UntrustedText(e.name or e.summary or e.key),
                due_date=due_dates.get(str(e.id)),
                done=e.done,
            )
            for e in data.epics
        ),
        tasks=tuple(tasks),
        transitions=tuple({(t.issue_id, t.entry_id): t for t in transitions}.values()),
        sprint_changes=tuple(sprint_changes),
        fetched_at=fetched_at,
    )
