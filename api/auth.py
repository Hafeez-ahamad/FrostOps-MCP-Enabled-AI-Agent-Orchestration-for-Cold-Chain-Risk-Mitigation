"""Application users, roles and bearer-token authentication.

Roles (spec section 10): dispatcher, supervisor, auditor, admin. A dispatcher may be limited
to some depots. Tokens are never stored: the directory keeps their sha256 and compares in
constant time.

Configuration: API_USERS in the environment, comma-separated entries
    user_id:role:depots:sha256_of_token          (depots: "*" or "DEP-LB|DEP-VER")
`python -m api` creates throwaway demo users with random tokens when API_USERS is not set and
prints the tokens once.
"""

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import Literal

Role = Literal["dispatcher", "supervisor", "auditor", "admin"]
ROLES: tuple[Role, ...] = ("dispatcher", "supervisor", "auditor", "admin")

# What each role may do (checked server side on every request).
PERMISSIONS: dict[str, frozenset[Role]] = {
    "read": frozenset(ROLES),
    "decide": frozenset({"dispatcher", "supervisor"}),
    "cancel": frozenset({"dispatcher", "supervisor"}),
    "control": frozenset({"supervisor", "admin"}),
    "policy_write": frozenset({"supervisor", "admin"}),
    "audit": frozenset({"auditor", "supervisor", "admin"}),
    "chat": frozenset(ROLES),
    "clock": frozenset({"supervisor", "admin"}),
}


@dataclass(frozen=True)
class User:
    user_id: str
    role: Role
    depots: frozenset[str] | None  # None means every depot

    def can(self, permission: str) -> bool:
        return self.role in PERMISSIONS[permission]

    def sees_depot(self, depot_id: str | None) -> bool:
        return self.depots is None or (depot_id is not None and depot_id in self.depots)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class UserDirectory:
    def __init__(self) -> None:
        self._by_hash: dict[str, User] = {}

    def add(self, user: User, token_sha256: str) -> None:
        if user.role not in ROLES:
            raise ValueError(f"unknown role {user.role}")
        self._by_hash[token_sha256] = user

    def authenticate(self, token: str) -> User | None:
        digest = token_hash(token)
        for known, user in self._by_hash.items():
            if hmac.compare_digest(known, digest):
                return user
        return None

    @classmethod
    def from_env(cls, value: str) -> "UserDirectory":
        directory = cls()
        for entry in filter(None, (e.strip() for e in value.split(","))):
            user_id, role, depots, digest = entry.split(":")
            scope = None if depots == "*" else frozenset(depots.split("|"))
            directory.add(User(user_id, role, scope), digest)  # type: ignore[arg-type]
        return directory


def demo_directory() -> tuple[UserDirectory, dict[str, str]]:
    """Throwaway users for a local demo: user id -> random token (shown once, never stored)."""
    directory = UserDirectory()
    tokens: dict[str, str] = {}
    for user in (
        User("dana", "dispatcher", frozenset({"DEP-SD", "DEP-LB"})),
        User("sam", "supervisor", None),
        User("ana", "auditor", None),
        User("adi", "admin", None),
    ):
        token = secrets.token_urlsafe(24)
        directory.add(user, token_hash(token))
        tokens[user.user_id] = token
    return directory, tokens
