"""HTTP clients for the integration services, with typed (Pydantic) responses.

Each call returns a `CallResult` instead of raising, so callers decide what a failure means:
- transport errors, 5xx and timeouts are retryable;
- a reply that fails validation (partial response) is retryable, because idempotency keys make
  the retry safe;
- 4xx business answers (facility full, unknown id) are not, except "driver_unreachable",
  which can change over time.
Latency is read from the X-Mock-Latency-Ms header when the service reports simulated latency,
otherwise measured, and counted against the client's timeout.
"""

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

LATENCY_HEADER = "X-Mock-Latency-Ms"
RETRYABLE_DETAILS = {"driver_unreachable"}
DEFAULT_TIMEOUT_MS = 2000.0

M = TypeVar("M", bound=BaseModel)


class HttpResponse(Protocol):
    @property
    def status_code(self) -> int: ...
    @property
    def headers(self) -> Mapping[str, str]: ...
    def json(self) -> Any: ...


class HttpClient(Protocol):
    """What the clients need from HTTP. httpx.Client satisfies it, and so does FastAPI's
    in-process TestClient used by tests and replay."""

    def request(
        self,
        method: str,
        url: str,
        *,
        json: Any | None = ...,
        params: Any | None = ...,
    ) -> HttpResponse: ...


@dataclass(frozen=True)
class CallResult:
    ok: bool
    retryable: bool = False
    error: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    latency_ms: float = 0.0


class ServiceClient:
    def __init__(self, http: HttpClient, timeout_ms: float = DEFAULT_TIMEOUT_MS) -> None:
        self.http = http
        self.timeout_ms = timeout_ms

    def call(
        self,
        method: str,
        path: str,
        model: type[M],
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> CallResult:
        started = time.perf_counter()
        try:
            response = self.http.request(method, path, json=json, params=params)
        except (httpx.HTTPError, OSError) as exc:
            return CallResult(False, retryable=True, error=f"unreachable:{type(exc).__name__}")
        reported = response.headers.get(LATENCY_HEADER)
        latency = float(reported) if reported else (time.perf_counter() - started) * 1000
        if latency > self.timeout_ms:
            return CallResult(False, retryable=True, error="timeout", latency_ms=latency)
        if response.status_code >= 500:
            return CallResult(False, True, f"http_{response.status_code}", latency_ms=latency)
        if response.status_code >= 400:
            try:
                detail = str(response.json().get("detail", f"http_{response.status_code}"))
            except ValueError:
                detail = f"http_{response.status_code}"
            return CallResult(False, detail in RETRYABLE_DETAILS, detail, latency_ms=latency)
        try:
            parsed = model.model_validate(response.json())
        except ValidationError:
            return CallResult(False, retryable=True, error="partial_response", latency_ms=latency)
        return CallResult(True, data=parsed.model_dump(mode="json"), latency_ms=latency)


# --- response models ---------------------------------------------------------------------


class Reachability(BaseModel):
    shipment_id: str
    reachable: bool


class Notification(BaseModel):
    notification_id: str
    delivered: bool


class Ticket(BaseModel):
    ticket_id: str
    status: str


class Escalation(BaseModel):
    escalation_id: str
    tier: str


class Diversion(BaseModel):
    diversion_id: str
    facility_id: str


class Cancellation(BaseModel):
    status: str


class FacilityStatus(BaseModel):
    facility_id: str
    name: str
    kind: str
    lat: float
    lon: float
    capacity: int
    available: int


class FacilityList(BaseModel):
    facilities: list[FacilityStatus]
    observed_at: datetime


class Weather(BaseModel):
    ambient_c: float
    wind_kmh: float
    observed_at: datetime


class Congestion(BaseModel):
    port_id: str
    level: float
    observed_at: datetime


# --- one client per service ----------------------------------------------------------------


class DriverClient(ServiceClient):
    def reachable(self, shipment_id: str) -> CallResult:
        return self.call("GET", f"/drivers/{shipment_id}/reachable", Reachability)

    def notify(self, shipment_id: str, message: str, idempotency_key: str) -> CallResult:
        body = {"shipment_id": shipment_id, "message": message, "idempotency_key": idempotency_key}
        return self.call("POST", "/notifications", Notification, json=body)


class TicketingClient(ServiceClient):
    def create_ticket(
        self, incident_id: str, shipment_id: str, summary: str, idempotency_key: str
    ) -> CallResult:
        body = {
            "incident_id": incident_id,
            "shipment_id": shipment_id,
            "summary": summary,
            "idempotency_key": idempotency_key,
        }
        return self.call("POST", "/tickets", Ticket, json=body)

    def close_ticket(self, ticket_id: str) -> CallResult:
        return self.call("POST", f"/tickets/{ticket_id}/close", Ticket)

    def escalate(
        self, incident_id: str, shipment_id: str, summary: str, idempotency_key: str
    ) -> CallResult:
        body = {
            "incident_id": incident_id,
            "shipment_id": shipment_id,
            "tier": "tier2",
            "summary": summary,
            "idempotency_key": idempotency_key,
        }
        return self.call("POST", "/escalations", Escalation, json=body)


class TmsClient(ServiceClient):
    def divert(self, shipment_id: str, facility_id: str, idempotency_key: str) -> CallResult:
        body = {
            "shipment_id": shipment_id,
            "facility_id": facility_id,
            "idempotency_key": idempotency_key,
        }
        return self.call("POST", "/diversions", Diversion, json=body)

    def cancel(self, diversion_id: str) -> CallResult:
        return self.call("POST", f"/diversions/{diversion_id}/cancel", Cancellation)


class FacilitiesClient(ServiceClient):
    def list(self) -> CallResult:
        return self.call("GET", "/facilities", FacilityList)


class WeatherClient(ServiceClient):
    def get(self, lat: float, lon: float) -> CallResult:
        return self.call("GET", "/weather", Weather, params={"lat": lat, "lon": lon})


class CongestionClient(ServiceClient):
    def get(self, port_id: str) -> CallResult:
        return self.call("GET", f"/ports/{port_id}/congestion", Congestion)


@dataclass
class Integrations:
    driver: DriverClient
    ticketing: TicketingClient
    tms: TmsClient
    facilities: FacilitiesClient
    weather: WeatherClient
    congestion: CongestionClient


class StaleCache:
    """Last good value per key. When a read fails, the cached value is returned marked stale,
    so callers can lower confidence instead of acting on silence (spec section 14)."""

    def __init__(self) -> None:
        self._values: dict[str, dict[str, Any]] = {}

    def read(self, key: str, result: CallResult) -> tuple[dict[str, Any] | None, bool]:
        """(value, stale). value is None if the read failed and nothing is cached."""
        if result.ok:
            self._values[key] = result.data
            return result.data, False
        return self._values.get(key), True
