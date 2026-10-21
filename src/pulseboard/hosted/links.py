"""Short-lived signed download links for in-memory exports (ADDENDUM-01 §1A.6, §7).

A link carries the export ID and a salted hash of the owner's Jira account, signed with a key
generated at process start, so links stop working on restart, after ``max_age_s``, or for any
export other than the one they were made for. The account itself is never in the link.
"""

import hashlib
import hmac
import secrets

from itsdangerous import BadSignature, URLSafeTimedSerializer

from pulseboard.domain.types import AccountId, ExportId

LINK_MAX_AGE_S = 900
"""15 minutes (ADDENDUM-01 §7)."""
EXPORTS_PATH = "/exports"
_KEY_BYTES = 32
_OWNER_TAG_CHARS = 32
_SIGNING_SALT = "pulseboard-export-link"


class ExportLinks:
    """Signs and checks export download tokens."""

    def __init__(self, max_age_s: int = LINK_MAX_AGE_S) -> None:
        self._serializer = URLSafeTimedSerializer(
            secrets.token_bytes(_KEY_BYTES), salt=_SIGNING_SALT
        )
        self._owner_salt = secrets.token_bytes(_KEY_BYTES)
        self._max_age_s = max_age_s

    def owner_tag(self, account: AccountId) -> str:
        """The salted hash that binds a link to its owner."""
        digest = hmac.new(self._owner_salt, account.encode(), hashlib.sha256).hexdigest()
        return digest[:_OWNER_TAG_CHARS]

    def sign(self, export_id: ExportId, owner: AccountId) -> str:
        """A token for ``export_id``, valid for its owner only."""
        return self._serializer.dumps([export_id, self.owner_tag(owner)])

    def path(self, export_id: ExportId, owner: AccountId) -> str:
        """The download path for ``export_id``."""
        return f"{EXPORTS_PATH}/{self.sign(export_id, owner)}"

    def verify(self, token: str) -> tuple[ExportId, str] | None:
        """``(export_id, owner_tag)`` for a valid, unexpired token; otherwise ``None``."""
        try:
            payload = self._serializer.loads(token, max_age=self._max_age_s)
        except BadSignature:  # SignatureExpired is a subclass
            return None
        if (
            not isinstance(payload, list)
            or len(payload) != 2  # noqa: PLR2004 - [export_id, owner_tag]
            or not all(isinstance(part, str) for part in payload)
        ):
            return None
        return ExportId(payload[0]), payload[1]

    def owned_by(self, tag: str, account: AccountId) -> bool:
        """Whether ``tag`` belongs to ``account`` (constant-time comparison)."""
        return hmac.compare_digest(tag, self.owner_tag(account))
