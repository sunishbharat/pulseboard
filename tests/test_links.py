"""Board link parsing bound to the site registry (DESIGN §9.1, §15.3)."""

import pytest

from network_guard import NetworkGuard
from pulseboard.domain.types import AuthMethod, Deployment, SiteId
from pulseboard.errors import ErrorCode, InvalidBoardLinkError, SiteNotConfiguredError
from pulseboard.links import parse_board_link
from pulseboard.sites import Site, SiteRegistry


@pytest.fixture
def registry(cloud_site: Site, dc_site: Site) -> SiteRegistry:
    return SiteRegistry([cloud_site, dc_site])


@pytest.mark.parametrize(
    ("url", "site_id", "board_id", "project"),
    [
        ("https://acme.atlassian.net/jira/software/projects/ABC/boards/42", "acme", 42, "ABC"),
        (
            "https://acme.atlassian.net/jira/software/c/projects/ABC/boards/7/backlog",
            "acme",
            7,
            "ABC",
        ),
        (
            "https://acme.atlassian.net/jira/software/projects/abc/boards/9/timeline?x=1#top",
            "acme",
            9,
            "ABC",
        ),
        ("https://ACME.Atlassian.NET./jira/software/c/boards/3", "acme", 3, None),
        ("https://acme.atlassian.net:443/jira/software/projects/A1_B/boards/5", "acme", 5, "A1_B"),
        (
            "https://acme.atlassian.net/secure/RapidBoard.jspa?rapidView=11&projectKey=XY",
            "acme",
            11,
            "XY",
        ),
        (
            "https://jira.corp.example:8443/jira/secure/RapidBoard.jspa?rapidView=12",
            "corp",
            12,
            None,
        ),
        (
            "https://jira.corp.example:8443/jira/secure/RapidBoard.jspa?view=planning&rapidView=13",
            "corp",
            13,
            None,
        ),
        (
            "https://jira.corp.example:8443/jira/jira/software/projects/OPS/boards/14",
            "corp",
            14,
            "OPS",
        ),
    ],
)
def test_link_shapes(
    registry: SiteRegistry, url: str, site_id: str, board_id: int, project: str | None
) -> None:
    ref = parse_board_link(url, registry)

    assert (ref.site_id, ref.board_id, ref.project_key) == (site_id, board_id, project)


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/jira/software/projects/ABC/boards/42",
        "https://acme.atlassian.net.evil.example/jira/software/projects/ABC/boards/42",
        "https://evilacme.atlassian.net/jira/software/projects/ABC/boards/42",
        "https://acme.atlassian.net:8443/jira/software/projects/ABC/boards/42",
        "https://jira.corp.example:8443/other/secure/RapidBoard.jspa?rapidView=1",
        "https://jira.corp.example:8443/jiraX/secure/RapidBoard.jspa?rapidView=1",
        "https://jira.corp.example/jira/secure/RapidBoard.jspa?rapidView=1",
    ],
)
def test_foreign_origin_rejected_without_network(
    registry: SiteRegistry, network_guard: NetworkGuard, url: str
) -> None:
    with pytest.raises(SiteNotConfiguredError) as info:
        parse_board_link(url, registry)

    assert info.value.code is ErrorCode.SITE_NOT_CONFIGURED
    assert info.value.hint is not None
    assert "pb_list_sites" in info.value.hint
    assert network_guard.attempts == []


def test_no_configured_sites_rejects_everything(network_guard: NetworkGuard) -> None:
    with pytest.raises(SiteNotConfiguredError):
        parse_board_link("https://acme.atlassian.net/jira/software/c/boards/1", SiteRegistry([]))
    assert network_guard.attempts == []


def test_http_rejected_unless_site_opts_in(registry: SiteRegistry) -> None:
    with pytest.raises(SiteNotConfiguredError, match="must use https://"):
        parse_board_link("http://acme.atlassian.net/jira/software/c/boards/1", registry)

    lab = Site(
        site_id=SiteId("lab"),
        base_url="http://jira.lab.local:8080",
        deployment=Deployment.DATA_CENTER,
        auth_method=AuthMethod.PAT,
        allow_http=True,
    )
    ref = parse_board_link(
        "http://jira.lab.local:8080/secure/RapidBoard.jspa?rapidView=4", SiteRegistry([lab])
    )
    assert ref.board_id == 4


@pytest.mark.parametrize(
    "url",
    [
        "https://user:pass@acme.atlassian.net/jira/software/c/boards/1",
        "https://acme.atlassian.net\\@evil.example/jira/software/c/boards/1",
        "ftp://acme.atlassian.net/jira/software/c/boards/1",
        "acme.atlassian.net/jira/software/c/boards/1",
        "https://acme.atlassian.net/jira/software/c/boards/1\n",
        "https:///jira/software/c/boards/1",
        "https://acme.atlassian.net:99999/jira/software/c/boards/1",
    ],
)
def test_unusable_urls_are_invalid_links(registry: SiteRegistry, url: str) -> None:
    with pytest.raises(InvalidBoardLinkError) as info:
        parse_board_link(url, registry)
    assert info.value.code is ErrorCode.INVALID_BOARD_LINK


@pytest.mark.parametrize(
    "url",
    [
        "https://acme.atlassian.net/browse/ABC-123",
        "https://acme.atlassian.net/jira/software/projects/ABC/boards/",
        "https://acme.atlassian.net/jira/software/projects/ABC/boards/abc",
        "https://acme.atlassian.net/jira/software/projects/ABC/boards/0",
        "https://acme.atlassian.net/jira/software/projects/ABC/boards/99999999999999999999",
        "https://acme.atlassian.net/jira/software/projects/ABC/boards/%34%32",
        "https://acme.atlassian.net/jira/software/projects/ABC/boards/٤٢",
        "https://acme.atlassian.net/secure/RapidBoard.jspa?projectKey=ABC",
    ],
)
def test_links_without_a_board_id(registry: SiteRegistry, url: str) -> None:
    with pytest.raises(InvalidBoardLinkError, match="board ID"):
        parse_board_link(url, registry)


def test_implausible_project_key_is_dropped(registry: SiteRegistry) -> None:
    ref = parse_board_link(
        "https://acme.atlassian.net/jira/software/projects/1-bad/boards/2", registry
    )
    assert ref.project_key is None


def test_most_specific_context_path_wins() -> None:
    def dc(site_id: str, base_url: str) -> Site:
        return Site(
            site_id=SiteId(site_id),
            base_url=base_url,
            deployment=Deployment.DATA_CENTER,
            auth_method=AuthMethod.PAT,
        )

    registry = SiteRegistry(
        [dc("root", "https://jira.example"), dc("nested", "https://jira.example/jira")]
    )
    page = "secure/RapidBoard.jspa?rapidView=1"

    assert parse_board_link(f"https://jira.example/jira/{page}", registry).site_id == "nested"
    assert parse_board_link(f"https://jira.example/{page}", registry).site_id == "root"


def test_registry_lookup(registry: SiteRegistry) -> None:
    assert [s.site_id for s in registry.sites] == ["acme", "corp"]
    assert registry.get(SiteId("corp")).deployment is Deployment.DATA_CENTER
    with pytest.raises(SiteNotConfiguredError):
        registry.get(SiteId("nope"))
