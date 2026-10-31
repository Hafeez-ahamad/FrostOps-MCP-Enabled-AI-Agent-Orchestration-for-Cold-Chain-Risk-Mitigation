"""Versioned policy updates with a safety check (PUT /admin/policy).

A new action catalogue is accepted only if it does not make any existing action *more*
autonomous: an L3 action may not become L2, an L4 action may not become executable, and an L2
action may not lose any of its preconditions. Making things stricter (or removing an action,
which the gate then denies by default) is always allowed. Every accepted version is stored in
ops.PolicyVersions with its hash, author and reason, and the change is audited first. The
catalogue loaded at startup is stored as the first version, and an update must carry a `version`
higher than the latest stored one.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Engine, func, insert, select

from engine.actions.policy import ActionCatalog, ActionLevel, parse_action_catalog
from engine.audit.log import AuditLog
from engine.data.schema import POLICY_VERSIONS

# How much an action may do without a person: higher means more autonomous.
AUTONOMY = {
    ActionLevel.L0: 0,
    ActionLevel.L1: 0,
    ActionLevel.L4: 0,
    ActionLevel.L3: 1,
    ActionLevel.L2: 2,
}


class PolicyRejected(ValueError):
    """The proposed policy would loosen autonomy or is invalid."""


def safety_problems(old: ActionCatalog, new: ActionCatalog) -> list[str]:
    problems = []
    for name, before in old.actions.items():
        after = new.get(name)
        if after is None:
            continue  # removed: unknown actions are denied by default
        if AUTONOMY[after.level] > AUTONOMY[before.level]:
            problems.append(f"{name}: {before.level.value} -> {after.level.value} loosens autonomy")
        if after.level is ActionLevel.L2 and not set(before.preconditions) <= set(
            after.preconditions
        ):
            missing = sorted(set(before.preconditions) - set(after.preconditions))
            problems.append(f"{name}: L2 action loses preconditions {missing}")
    for name, after in new.actions.items():
        if (
            name not in old.actions
            and after.level is ActionLevel.L2
            and "data_quality_ok" not in after.preconditions
        ):
            problems.append(f"{name}: a new L2 action must require data_quality_ok")
    return problems


@dataclass
class PolicyAdmin:
    engine: Engine
    catalog: ActionCatalog
    yaml_text: str
    now: datetime

    def __post_init__(self) -> None:
        if self.catalog.version > self._latest_version():
            self._store(
                self.catalog, self.yaml_text, "system", "initial policy at startup", self.now
            )

    def _latest_version(self) -> int:
        with self.engine.connect() as conn:
            return (
                conn.execute(select(func.max(POLICY_VERSIONS.c.Version))).scalar_one_or_none() or 0
            )

    def _store(
        self, catalog: ActionCatalog, yaml_text: str, author: str, reason: str, now: datetime
    ) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                insert(POLICY_VERSIONS),
                {
                    "Version": catalog.version,
                    "PolicyHash": catalog.policy_hash,
                    "YamlText": yaml_text,
                    "Author": author,
                    "Reason": reason[:400],
                    "CreatedAt": now,
                },
            )

    def current(self) -> dict[str, Any]:
        with self.engine.connect() as conn:
            versions = [
                {k: v for k, v in dict(r).items() if k != "YamlText"}
                for r in conn.execute(
                    select(POLICY_VERSIONS).order_by(POLICY_VERSIONS.c.Version)
                ).mappings()
            ]
        return {
            "version": self.catalog.version,
            "policy_hash": self.catalog.policy_hash,
            "yaml_text": self.yaml_text,
            "history": versions,
        }

    def update(self, yaml_text: str, author: str, reason: str, now: datetime) -> ActionCatalog:
        try:
            new = parse_action_catalog(yaml_text)
        except Exception as exc:  # noqa: BLE001 - any parse/validation error is a rejection
            raise PolicyRejected(f"invalid policy: {exc}") from exc
        problems = safety_problems(self.catalog, new)
        if problems:
            raise PolicyRejected("; ".join(problems))
        latest = self._latest_version()
        if new.version <= latest:
            raise PolicyRejected(f"policy version {new.version} must be higher than {latest}")
        AuditLog(self.engine).append(
            "policy_updated",
            author,
            now,
            detail={
                "from_hash": self.catalog.policy_hash,
                "to_hash": new.policy_hash,
                "reason": reason,
            },
        )
        self._store(new, yaml_text, author, reason, now)
        self.catalog, self.yaml_text = new, yaml_text
        return new
