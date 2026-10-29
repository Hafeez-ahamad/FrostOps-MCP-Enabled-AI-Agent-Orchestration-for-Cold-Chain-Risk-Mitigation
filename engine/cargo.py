"""Cargo types and temperature bands, loaded from policy/cargo_types.yaml."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from engine.settings import PROJECT_ROOT

CARGO_TYPES_PATH = PROJECT_ROOT / "policy" / "cargo_types.yaml"


class CargoType(BaseModel, frozen=True):
    code: str
    name: str
    min_c: float
    max_c: float
    setpoint_c: float
    source: Literal["sop", "config_assumption"]
    sop_clause: str | None
    share: float
    sensitivity: float = Field(ge=0.0, le=1.0)  # weight in the risk score

    @model_validator(mode="after")
    def _check_band(self) -> "CargoType":
        if not self.min_c < self.setpoint_c < self.max_c:
            raise ValueError(f"{self.code}: setpoint must lie strictly inside the band")
        if self.source == "sop" and not self.sop_clause:
            raise ValueError(f"{self.code}: an SOP-sourced band needs its clause")
        return self


def load_cargo_types(path: Path = CARGO_TYPES_PATH) -> dict[str, CargoType]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))["cargo_types"]
    types = {code: CargoType(code=code, **spec) for code, spec in raw.items()}
    if abs(sum(t.share for t in types.values()) - 1.0) > 1e-9:
        raise ValueError("cargo type shares must sum to 1")
    return types
