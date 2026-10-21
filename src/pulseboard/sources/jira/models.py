"""Typed models of the Jira REST responses Pulseboard reads (DESIGN §9.3).

Parsed in strict mode straight from response bytes; raw dicts never leave the Jira adapter.
Unknown fields are ignored. Where Cloud and Data Center differ, both shapes are accepted.
"""

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, JsonValue, RootModel

_JIRA = ConfigDict(frozen=True, strict=True, extra="ignore", populate_by_name=True)


class _Model(BaseModel):
    model_config = _JIRA


class JiraUser(_Model):
    """``/myself`` or an assignee. Cloud has ``accountId``; Data Center has ``key``."""

    account_id: str | None = Field(default=None, alias="accountId")
    key: str | None = None
    name: str | None = None
    display_name: str | None = Field(default=None, alias="displayName")
    time_zone: str | None = Field(default=None, alias="timeZone")

    @property
    def identity(self) -> str | None:
        """The stable account identity: Cloud account ID, else Data Center user key."""
        return self.account_id or self.key


class JiraServerInfo(_Model):
    """``/rest/api/2/serverInfo``: readable without signing in on both deployments."""

    deployment_type: str | None = Field(default=None, alias="deploymentType")
    """``Cloud``, ``Server`` or ``DataCenter``."""
    version: str | None = None
    server_title: str | None = Field(default=None, alias="serverTitle")


class JiraBoard(_Model):
    """``/rest/agile/1.0/board/{id}``."""

    id: int
    name: str
    type: str


class JiraStatusRef(_Model):
    """A status reference in board configuration."""

    id: str


class JiraColumn(_Model):
    """A board column."""

    name: str
    statuses: tuple[JiraStatusRef, ...] = ()


class JiraColumnConfig(_Model):
    """Board column configuration."""

    columns: tuple[JiraColumn, ...] = ()


class JiraEstimationField(_Model):
    """The field a board estimates with."""

    field_id: str = Field(alias="fieldId")


class JiraEstimation(_Model):
    """Board estimation configuration (``field`` or ``issueCount``)."""

    type: str
    field: JiraEstimationField | None = None


class JiraBoardConfig(_Model):
    """``/rest/agile/1.0/board/{id}/configuration``."""

    column_config: JiraColumnConfig = Field(alias="columnConfig")
    estimation: JiraEstimation | None = None


class JiraSprint(_Model):
    """A sprint, from the board sprint list or an issue's agile ``sprint`` field."""

    id: int
    state: str
    name: str
    start_date: datetime | None = Field(default=None, alias="startDate")
    end_date: datetime | None = Field(default=None, alias="endDate")
    goal: str | None = None


class JiraVersion(_Model):
    """A board version. ``releaseDate`` is a date or a timestamp depending on deployment."""

    id: int | str
    name: str
    released: bool = False
    release_date: date | datetime | None = Field(default=None, alias="releaseDate")


class JiraEpic(_Model):
    """A board epic (the agile resource has no due date)."""

    id: int
    key: str
    name: str | None = None
    summary: str | None = None
    done: bool = False


class JiraStatusCategory(_Model):
    """``new``, ``indeterminate`` or ``done``."""

    key: str


class JiraStatus(_Model):
    """A status, from ``/rest/api/2/status`` or an issue."""

    id: str
    name: str
    status_category: JiraStatusCategory = Field(alias="statusCategory")


class JiraStatusList(RootModel[tuple[JiraStatus, ...]]):
    """``/rest/api/2/status``: a top-level array."""

    model_config = ConfigDict(frozen=True, strict=True)


class JiraIssueType(_Model):
    """An issue type."""

    name: str
    subtask: bool = False


class JiraNamed(_Model):
    """Anything with a name (priority, link type)."""

    name: str


class JiraLinkType(_Model):
    """An issue link type, such as ``Blocks``."""

    name: str
    inward: str | None = None
    outward: str | None = None


class JiraLinkedStatus(_Model):
    """The status of a linked issue."""

    status: JiraStatus | None = None


class JiraLinkedIssue(_Model):
    """The other end of an issue link."""

    id: str
    key: str
    fields: JiraLinkedStatus = JiraLinkedStatus()


class JiraLink(_Model):
    """An issue link. ``inwardIssue`` set means "this issue <inward> that one"."""

    type: JiraLinkType
    inward_issue: JiraLinkedIssue | None = Field(default=None, alias="inwardIssue")
    outward_issue: JiraLinkedIssue | None = Field(default=None, alias="outwardIssue")


class JiraParentFields(_Model):
    """Fields of a parent issue."""

    issuetype: JiraIssueType | None = None


class JiraParent(_Model):
    """An issue's parent (epic in Cloud, or the story of a subtask)."""

    id: str
    key: str
    fields: JiraParentFields = JiraParentFields()


class JiraEpicRef(_Model):
    """The agile ``epic`` field of an issue."""

    id: int
    key: str


class JiraVersionRef(_Model):
    """A fix version on an issue."""

    id: str


class JiraFields(BaseModel):
    """Issue fields. Extra fields (the estimation and rank custom fields) are kept raw."""

    model_config = ConfigDict(frozen=True, strict=True, extra="allow", populate_by_name=True)

    summary: str | None = None
    status: JiraStatus
    assignee: JiraUser | None = None
    issuetype: JiraIssueType
    priority: JiraNamed | None = None
    issuelinks: tuple[JiraLink, ...] = ()
    duedate: date | None = None
    created: datetime
    updated: datetime | None = None
    resolutiondate: datetime | None = None
    parent: JiraParent | None = None
    sprint: JiraSprint | None = None
    epic: JiraEpicRef | None = None
    fix_versions: tuple[JiraVersionRef, ...] = Field(default=(), alias="fixVersions")
    timeoriginalestimate: int | None = None
    timeestimate: int | None = None

    def custom_value(self, field_id: str) -> JsonValue:
        """A raw extra field value, or ``None``."""
        return (self.model_extra or {}).get(field_id)


class JiraIssue(_Model):
    """An issue from the board issue endpoint."""

    id: str
    key: str
    fields: JiraFields


class JiraIssuePage(_Model):
    """A page of board issues (offset paging)."""

    issues: tuple[JiraIssue, ...] = ()
    start_at: int = Field(default=0, alias="startAt")
    max_results: int = Field(default=0, alias="maxResults")
    total: int = 0


class JiraValuesPage[T](_Model):
    """A page of agile values (sprints, versions, epics, Cloud changelog)."""

    values: tuple[T, ...] = ()
    start_at: int = Field(default=0, alias="startAt")
    max_results: int = Field(default=0, alias="maxResults")
    is_last: bool = Field(default=True, alias="isLast")


class JiraChangeItem(_Model):
    """One field change in a history entry. Status and sprint changes carry IDs."""

    field: str
    from_value: str | None = Field(default=None, alias="from")
    to_value: str | None = Field(default=None, alias="to")


class JiraHistory(_Model):
    """A changelog history entry."""

    id: str
    created: datetime
    items: tuple[JiraChangeItem, ...] = ()


class JiraChangelog(_Model):
    """The ``changelog`` of a Data Center issue fetched with ``expand=changelog``."""

    histories: tuple[JiraHistory, ...] = ()


class JiraIssueWithChangelog(_Model):
    """A Data Center issue with its changelog."""

    id: str
    changelog: JiraChangelog = JiraChangelog()


class JiraDueDateFields(_Model):
    """Fields fetched for epics."""

    duedate: date | None = None


class JiraDueDateIssue(_Model):
    """An epic with its due date, from a JQL search."""

    id: str
    key: str
    fields: JiraDueDateFields = JiraDueDateFields()


class JiraSearchPage(_Model):
    """A JQL search page: token paging on Cloud, offset paging on Data Center."""

    issues: tuple[JiraDueDateIssue, ...] = ()
    next_page_token: str | None = Field(default=None, alias="nextPageToken")
    start_at: int = Field(default=0, alias="startAt")
    total: int | None = None


class JiraFieldSchema(_Model):
    """A field's schema."""

    custom: str | None = None


class JiraField(_Model):
    """A field from the field catalogue."""

    id: str
    name: str
    custom: bool = False
    schema_: JiraFieldSchema | None = Field(default=None, alias="schema")


class JiraFieldList(RootModel[tuple[JiraField, ...]]):
    """``/rest/api/2/field``: a top-level array."""

    model_config = ConfigDict(frozen=True, strict=True)


class JiraBoardData(_Model):
    """Everything fetched for one board."""

    myself: JiraUser
    board: JiraBoard
    configuration: JiraBoardConfig
    sprints: tuple[JiraSprint, ...]
    versions: tuple[JiraVersion, ...]
    epics: tuple[JiraEpic, ...]
    epic_due_dates: tuple[JiraDueDateIssue, ...]
    issues: tuple[JiraIssue, ...]
    histories: dict[str, tuple[JiraHistory, ...]]
    """Changelog histories by issue ID."""
    statuses: tuple[JiraStatus, ...]
    fields: tuple[JiraField, ...]
