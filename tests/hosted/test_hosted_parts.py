"""Profile A building blocks: credential headers, identity cache, signed links, memory caps."""

import time

import pytest
from pydantic import SecretStr

from factories import fixture_board
from pulseboard.credentials import Credentials
from pulseboard.domain.types import AccountId, AuthMethod, Deployment, ExportId, SiteId
from pulseboard.errors import AuthFailedError, BoardTooLargeError, DurableStorageRequiredError
from pulseboard.hosted.access import AccessKeyMiddleware
from pulseboard.hosted.caller import credentials_from_headers, current_caller
from pulseboard.hosted.identity import IdentityCache
from pulseboard.hosted.links import ExportLinks
from pulseboard.hosted.memory import MemoryFiles, MemoryLimits, MemoryStore
from pulseboard.mcp.server import error_text
from pulseboard.sites import Site

ALICE = Credentials(email="alice@example.com", token=SecretStr("alice-token"))


class Timer:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def dc_hosted_site() -> Site:
    return Site(
        site_id=SiteId("jira"),
        base_url="https://jira.corp.example/jira",
        deployment=Deployment.DATA_CENTER,
        auth_method=AuthMethod.PAT,
    )


# ---------------------------------------------------------------- headers


def test_cloud_headers_are_case_insensitive_and_secret(cloud_site: Site) -> None:
    creds = credentials_from_headers(
        {"x-jira-user-email": "alice@example.com", "X-JIRA-API-TOKEN": "alice-token"}, cloud_site
    )

    assert creds.email == "alice@example.com"
    assert creds.token is not None
    assert creds.token.get_secret_value() == "alice-token"
    assert "alice-token" not in repr(creds)


def test_data_center_needs_a_pat(dc_hosted_site: Site) -> None:
    creds = credentials_from_headers({"X-Jira-Pat": "dc-pat"}, dc_hosted_site)

    assert creds.email is None
    with pytest.raises(AuthFailedError, match="missing per-user Jira credentials"):
        # Cloud headers do not stand in for a PAT, and vice versa.
        credentials_from_headers(
            {"X-Jira-User-Email": "a@example.com", "X-Jira-Api-Token": "t"}, dc_hosted_site
        )


def test_pat_alone_is_half_a_cloud_login(cloud_site: Site) -> None:
    with pytest.raises(AuthFailedError, match="missing per-user Jira credentials"):
        credentials_from_headers({"X-Jira-Pat": "p", "X-Jira-Api-Token": "t"}, cloud_site)


def test_no_caller_outside_a_request() -> None:
    with pytest.raises(AuthFailedError, match="missing per-user Jira credentials"):
        current_caller()


# ---------------------------------------------------------------- identity


def test_identity_is_cached_for_ten_minutes_by_salted_hash() -> None:
    timer, lookups = Timer(), []
    cache = IdentityCache(timer=timer)

    def lookup() -> AccountId:
        lookups.append(1)
        return AccountId("acc-alice")

    assert cache.resolve(ALICE, lookup) == "acc-alice"
    assert cache.resolve(ALICE, lookup) == "acc-alice"
    timer.now += 601
    assert cache.resolve(ALICE, lookup) == "acc-alice"
    assert len(lookups) == 2
    key = cache.key(ALICE)
    assert "alice-token" not in key
    assert key != IdentityCache().key(ALICE)  # the salt is new per process start


def test_different_tokens_never_share_an_identity() -> None:
    cache = IdentityCache()
    other = Credentials(email="alice@example.com", token=SecretStr("another-token"))

    cache.resolve(ALICE, lambda: AccountId("acc-alice"))

    assert cache.resolve(other, lambda: AccountId("acc-mallory")) == "acc-mallory"


def test_failed_lookups_are_not_cached() -> None:
    cache = IdentityCache()

    def rejected() -> AccountId:
        msg = "Jira rejected the credentials."
        raise AuthFailedError(msg)

    with pytest.raises(AuthFailedError):
        cache.resolve(ALICE, rejected)
    assert cache.resolve(ALICE, lambda: AccountId("acc-alice")) == "acc-alice"


# ---------------------------------------------------------------- access key


def test_access_key_check() -> None:
    keys = [SecretStr("team-a-access-key-0123"), SecretStr("team-b-access-key-4567")]
    check = AccessKeyMiddleware(lambda *_: None, keys=keys)  # type: ignore[arg-type]

    assert check.allows(b"team-a-access-key-0123")
    assert check.allows(b"team-b-access-key-4567")
    assert not check.allows(b"team-a-access-key-012")
    assert not check.allows(b"")
    assert not check.allows(None)


# ---------------------------------------------------------------- signed links


def test_links_round_trip_and_reject_tampering(monkeypatch: pytest.MonkeyPatch) -> None:
    links = ExportLinks(max_age_s=60)
    token = links.sign(ExportId("e1"), AccountId("acc-alice"))

    export_id, tag = links.verify(token) or ("", "")
    assert export_id == "e1"
    assert links.owned_by(tag, AccountId("acc-alice"))
    assert not links.owned_by(tag, AccountId("acc-bob"))
    assert "acc-alice" not in token
    # The payload's first character; a signature's last one may only carry padding bits.
    assert links.verify(("X" if token[0] != "X" else "Y") + token[1:]) is None
    assert ExportLinks().verify(token) is None  # another process start, another key

    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 61)
    assert links.verify(token) is None  # expired


# ---------------------------------------------------------------- memory


def test_memory_store_expires_and_caps_plans_and_snapshots() -> None:
    timer = Timer()
    store = MemoryStore(MemoryLimits(ttl_s=60, max_snapshots=2), timer)
    board = fixture_board()
    ids = [store.save_snapshot(board, full_reload=True) for _ in range(3)]

    assert store.get_snapshot(ids[0]) is None  # least recently used, evicted
    assert store.get_snapshot(ids[2]) is not None
    timer.now += 61
    assert store.get_snapshot(ids[2]) is None  # expired


def test_memory_store_has_no_durable_features() -> None:
    store = MemoryStore()
    board = fixture_board()

    assert store.has_baseline(board.site_id, board.account_id, board.board_id, "sprint:11")
    assert store.known_account(board.site_id, board.board_id) is None
    assert store.latest_plan(board.site_id, board.account_id, (board.board_id,), "x") is None


def test_snapshots_are_found_only_for_their_account() -> None:
    store = MemoryStore()
    board = fixture_board()
    store.save_snapshot(board, full_reload=True)
    args = (board.site_id, board.board_id, board.snapshot_hash)

    assert store.snapshot_by_hash(args[0], board.account_id, args[1], args[2]) == board
    assert store.snapshot_by_hash(args[0], AccountId("acc-other"), args[1], args[2]) is None


def test_memory_files_are_capped_by_size() -> None:
    files = MemoryFiles(MemoryLimits(max_export_bytes=10))
    files.write("a", b"12345")
    files.write("b", b"67890")
    files.write("c", b"abcde")

    assert files.read("a") is None  # evicted to stay within 10 bytes
    assert files.read("c") == b"abcde"
    with pytest.raises(BoardTooLargeError, match="in-memory limit"):
        files.write("d", b"x" * 11)


# ---------------------------------------------------------------- errors
def test_durable_storage_features_are_not_available() -> None:
    error = DurableStorageRequiredError("Baseline variance")

    assert error_text(error) == (
        "not_available: Baseline variance requires durable storage, which this hosted server"
        " does not have. Hint: Run Pulseboard locally (stdio) for this feature."
    )
