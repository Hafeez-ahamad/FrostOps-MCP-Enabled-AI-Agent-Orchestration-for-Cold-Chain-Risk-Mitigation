"""Fault injection for the mock services.

Faults are queued per endpoint and consumed one call at a time, or set as a standing mode:

    ok        normal response
    fail      HTTP 503
    slow      normal response, but reports `latency_ms` of simulated latency in the
              X-Mock-Latency-Ms header; the client counts it against its timeout (simulated
              time, so tests never sleep)
    partial   HTTP 200 with required fields missing
"""

from dataclasses import dataclass, field
from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel

FaultMode = Literal["ok", "fail", "slow", "partial"]
LATENCY_HEADER = "X-Mock-Latency-Ms"


class Fault(BaseModel, frozen=True):
    mode: FaultMode = "ok"
    latency_ms: float = 0.0


@dataclass
class FaultInjector:
    queued: dict[str, list[Fault]] = field(default_factory=dict)
    standing: dict[str, Fault] = field(default_factory=dict)

    def queue(self, endpoint: str, *faults: Fault) -> None:
        self.queued.setdefault(endpoint, []).extend(faults)

    def set(self, endpoint: str, fault: Fault | None) -> None:
        if fault is None:
            self.standing.pop(endpoint, None)
        else:
            self.standing[endpoint] = fault

    def clear(self) -> None:
        self.queued.clear()
        self.standing.clear()

    def next(self, endpoint: str) -> Fault:
        pending = self.queued.get(endpoint)
        if pending:
            return pending.pop(0)
        return self.standing.get(endpoint, Fault())


class FaultRequest(BaseModel):
    endpoint: str
    faults: list[Fault] = []
    standing: Fault | None = None


def add_fault_admin(app: FastAPI, faults: FaultInjector) -> None:
    """POST /_faults to inject faults into a running mock (manual demos)."""

    @app.post("/_faults")
    def set_faults(req: FaultRequest) -> dict[str, str]:
        if req.faults:
            faults.queue(req.endpoint, *req.faults)
        faults.set(req.endpoint, req.standing)
        return {"status": "ok"}

    @app.delete("/_faults")
    def clear_faults() -> dict[str, str]:
        faults.clear()
        return {"status": "cleared"}
