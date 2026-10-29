"""The action catalogue (policy/actions.yaml) as typed, validated models."""

import hashlib
from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from engine.settings import PROJECT_ROOT

ACTIONS_PATH = PROJECT_ROOT / "policy" / "actions.yaml"

Precondition = Literal[
    "rule_breach_confirmed",
    "data_quality_ok",
    "driver_reachable",
    "facility_capacity_ok",
    "eta_delay_gt_60min",
]
Integration = Literal["driver_notify", "ticketing", "tms_diversion", "escalation"]


class ActionLevel(StrEnum):
    L0 = "L0"  # observe
    L1 = "L1"  # recommend
    L2 = "L2"  # auto-execute within limits
    L3 = "L3"  # needs a named human approval
    L4 = "L4"  # never autonomous


class Reversibility(StrEnum):
    YES = "yes"
    NO = "no"
    UNTIL_DEPARTURE = "until_departure"


class ActionPolicy(BaseModel, frozen=True):
    name: str
    level: ActionLevel
    reversible: Reversibility
    preconditions: tuple[Precondition, ...]
    cooldown_minutes: float = 0.0
    max_per_shipment_per_hour: int | None = None
    approver_roles: tuple[str, ...] = ()
    integration: Integration | None = None

    @model_validator(mode="after")
    def _consistent(self) -> "ActionPolicy":
        if self.level is ActionLevel.L3 and not self.approver_roles:
            raise ValueError(f"{self.name}: an L3 action needs approver_roles")
        if self.level in (ActionLevel.L2, ActionLevel.L3) and self.integration is None:
            raise ValueError(f"{self.name}: an executable action needs an integration")
        return self


class GlobalLimits(BaseModel, frozen=True):
    max_auto_actions_per_hour: int = Field(ge=0)
    kill_switch: bool
    dry_run: bool
    approval_ttl_minutes: float = Field(gt=0)


class ActionCatalog(BaseModel, frozen=True):
    version: int
    actions: dict[str, ActionPolicy]
    global_limits: GlobalLimits
    policy_hash: str  # sha256 of the YAML text: which policy version made a decision

    def get(self, name: str) -> ActionPolicy | None:
        return self.actions.get(name)


def load_action_catalog(path: Path = ACTIONS_PATH) -> ActionCatalog:
    return parse_action_catalog(path.read_text(encoding="utf-8"))


def parse_action_catalog(text: str) -> ActionCatalog:
    """Validate a catalogue given as YAML text (used for files and for PUT /admin/policy)."""
    raw = yaml.safe_load(text)
    if not isinstance(raw, dict) or "actions" not in raw or "global_limits" not in raw:
        raise ValueError("a policy needs `version`, `actions` and `global_limits`")
    for name, spec in raw["actions"].items():
        if "preconditions" not in spec:
            raise ValueError(f"{name}: preconditions must be listed explicitly (may be [])")
    actions = {name: ActionPolicy(name=name, **spec) for name, spec in raw["actions"].items()}
    return ActionCatalog(
        version=raw["version"],
        actions=actions,
        global_limits=GlobalLimits(**raw["global_limits"]),
        policy_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )
