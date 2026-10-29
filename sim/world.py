"""Static world: depots, ports, destinations, facilities and cargo types."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel

from engine.cargo import CargoType, load_cargo_types
from engine.geo import haversine_km

WORLD_PATH = Path(__file__).resolve().parent / "config" / "world.yaml"


class Place(BaseModel, frozen=True):
    id: str
    name: str
    lat: float
    lon: float


class Depot(Place, frozen=True):
    inland: bool


class Port(Place, frozen=True):
    pass


class Destination(Place, frozen=True):
    port_id: str | None = None


class Facility(Place, frozen=True):
    capacity: int
    kind: Literal["cold_storage", "overflow_depot"]


class World(BaseModel, frozen=True):
    depots: list[Depot]
    ports: list[Port]
    destinations: list[Destination]
    facilities: list[Facility]
    cargo_types: dict[str, CargoType]

    def depot(self, depot_id: str) -> Depot:
        return next(d for d in self.depots if d.id == depot_id)

    def facility(self, facility_id: str) -> Facility:
        return next(f for f in self.facilities if f.id == facility_id)

    def cold_storage_by_distance(self, lat: float, lon: float) -> list[Facility]:
        """Emergency cold-storage sites, nearest first (ties broken by id)."""
        sites = [f for f in self.facilities if f.kind == "cold_storage"]
        return sorted(sites, key=lambda f: (haversine_km(lat, lon, f.lat, f.lon), f.id))

    def overflow_depot(self) -> Facility:
        return next(f for f in self.facilities if f.kind == "overflow_depot")


def load_world(path: Path = WORLD_PATH) -> World:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return World(**raw, cargo_types=load_cargo_types())
