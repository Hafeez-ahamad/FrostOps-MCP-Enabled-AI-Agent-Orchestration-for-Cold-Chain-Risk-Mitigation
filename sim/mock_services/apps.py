"""The six mock integration services as FastAPI apps.

Every write endpoint takes an idempotency key: a retry with the same key gets the stored
response and causes no second side effect. An injected `fail` fault is applied before any
side effect; a `partial` fault is applied after it (the effect happened, the reply is broken),
which is exactly the case idempotent retries exist for.
"""

from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from sim.mock_services.faults import LATENCY_HEADER, Fault, FaultInjector, add_fault_admin
from sim.mock_services.state import MockWorld


def _fail() -> JSONResponse:
    return JSONResponse({"detail": "injected failure"}, status_code=503)


def _reply(fault: Fault, body: dict[str, Any], status: int = 200) -> JSONResponse:
    headers = {LATENCY_HEADER: str(fault.latency_ms)} if fault.mode == "slow" else {}
    if fault.mode == "partial" and status == 200:
        body = {"partial": True}
    return JSONResponse(body, status_code=status, headers=headers)


def _once(world: MockWorld, key: str, effect: Any) -> tuple[dict[str, Any], int]:
    """Run `effect()` -> (body, status) at most once per idempotency key."""
    if key in world.responses:
        stored = world.responses[key]
        return stored["body"], stored["status"]
    body, status = effect()
    if status < 500:
        world.responses[key] = {"body": body, "status": status}
    return body, status


# --- driver notification -----------------------------------------------------------------


class NotifyRequest(BaseModel):
    shipment_id: str
    message: str
    idempotency_key: str


def create_driver_notify_app(world: MockWorld, faults: FaultInjector) -> FastAPI:
    app = FastAPI(title="Mock driver notification (simulated)")
    add_fault_admin(app, faults)

    @app.get("/drivers/{shipment_id}/reachable")
    def reachable(shipment_id: str) -> JSONResponse:
        fault = faults.next("driver.reachable")
        if fault.mode == "fail":
            return _fail()
        body = {"shipment_id": shipment_id, "reachable": world.driver_reachable(shipment_id)}
        return _reply(fault, body)

    @app.post("/notifications")
    def notify(req: NotifyRequest) -> JSONResponse:
        fault = faults.next("driver.notify")
        if fault.mode == "fail":
            return _fail()

        # An unreachable driver may be reachable later, so that answer is not stored.
        if not world.driver_reachable(req.shipment_id):
            return _reply(fault, {"detail": "driver_unreachable"}, 409)

        def effect() -> tuple[dict[str, Any], int]:
            world.log.append({"service": "driver", "shipment": req.shipment_id, "msg": req.message})
            return {"notification_id": f"NTF-{len(world.log):05d}", "delivered": True}, 200

        body, status = _once(world, req.idempotency_key, effect)
        return _reply(fault, body, status)

    return app


# --- ticketing and escalation ---------------------------------------------------------------


class TicketRequest(BaseModel):
    incident_id: str
    shipment_id: str
    summary: str
    priority: str = "normal"
    idempotency_key: str


class EscalationRequest(BaseModel):
    incident_id: str
    shipment_id: str
    tier: str = "tier2"
    summary: str
    idempotency_key: str


def create_ticketing_app(world: MockWorld, faults: FaultInjector) -> FastAPI:
    app = FastAPI(title="Mock ticketing and escalation (simulated)")
    add_fault_admin(app, faults)
    open_tickets: dict[str, str] = {}

    @app.post("/tickets")
    def create_ticket(req: TicketRequest) -> JSONResponse:
        fault = faults.next("ticketing.create")
        if fault.mode == "fail":
            return _fail()

        def effect() -> tuple[dict[str, Any], int]:
            ticket_id = f"TKT-{len(open_tickets) + 1:05d}"
            open_tickets[ticket_id] = "open"
            world.log.append(
                {"service": "ticketing", "ticket": ticket_id, "incident": req.incident_id}
            )
            return {"ticket_id": ticket_id, "status": "open"}, 200

        body, status = _once(world, req.idempotency_key, effect)
        return _reply(fault, body, status)

    @app.post("/tickets/{ticket_id}/close")
    def close_ticket(ticket_id: str) -> JSONResponse:
        fault = faults.next("ticketing.close")
        if fault.mode == "fail":
            return _fail()
        if ticket_id not in open_tickets:
            return _reply(fault, {"detail": "unknown_ticket"}, 404)
        open_tickets[ticket_id] = "closed"
        return _reply(fault, {"ticket_id": ticket_id, "status": "closed"})

    @app.post("/escalations")
    def escalate(req: EscalationRequest) -> JSONResponse:
        fault = faults.next("ticketing.escalate")
        if fault.mode == "fail":
            return _fail()

        def effect() -> tuple[dict[str, Any], int]:
            escalation_id = f"ESC-{len(world.log) + 1:05d}"
            world.log.append(
                {"service": "escalation", "incident": req.incident_id, "tier": req.tier}
            )
            return {"escalation_id": escalation_id, "tier": req.tier}, 200

        body, status = _once(world, req.idempotency_key, effect)
        return _reply(fault, body, status)

    return app


# --- TMS diversion ----------------------------------------------------------------------------


class DiversionRequest(BaseModel):
    shipment_id: str
    facility_id: str
    idempotency_key: str


def create_tms_app(world: MockWorld, faults: FaultInjector) -> FastAPI:
    app = FastAPI(title="Mock TMS diversion (simulated)")
    add_fault_admin(app, faults)
    diversions: dict[str, dict[str, str]] = {}

    @app.post("/diversions")
    def divert(req: DiversionRequest) -> JSONResponse:
        fault = faults.next("tms.divert")
        if fault.mode == "fail":
            return _fail()

        def effect() -> tuple[dict[str, Any], int]:
            try:
                capacity, occupied = world.facility_occupancy(req.facility_id)
            except StopIteration:
                return {"detail": "unknown_facility"}, 404
            if occupied >= capacity:
                return {"detail": "facility_full"}, 409
            world.reservations.setdefault(req.facility_id, set()).add(req.shipment_id)
            diversion_id = f"DIV-{len(diversions) + 1:05d}"
            diversions[diversion_id] = {"shipment": req.shipment_id, "facility": req.facility_id}
            world.log.append(
                {"service": "tms", "diversion": diversion_id, **diversions[diversion_id]}
            )
            return {"diversion_id": diversion_id, "facility_id": req.facility_id}, 200

        body, status = _once(world, req.idempotency_key, effect)
        return _reply(fault, body, status)

    @app.post("/diversions/{diversion_id}/cancel")
    def cancel(diversion_id: str) -> JSONResponse:
        fault = faults.next("tms.cancel")
        if fault.mode == "fail":
            return _fail()
        record = diversions.pop(diversion_id, None)
        if record is None:
            return _reply(fault, {"detail": "unknown_diversion"}, 404)
        world.reservations.get(record["facility"], set()).discard(record["shipment"])
        return _reply(fault, {"diversion_id": diversion_id, "status": "cancelled"})

    return app


# --- facilities, weather, congestion (read-only) --------------------------------------------


def create_facilities_app(world: MockWorld, faults: FaultInjector) -> FastAPI:
    app = FastAPI(title="Mock facilities (simulated)")
    add_fault_admin(app, faults)

    @app.get("/facilities")
    def facilities() -> JSONResponse:
        fault = faults.next("facilities.list")
        if fault.mode == "fail":
            return _fail()
        items = []
        for f in world.world.facilities:
            capacity, occupied = world.facility_occupancy(f.id)
            items.append(
                {
                    "facility_id": f.id,
                    "name": f.name,
                    "kind": f.kind,
                    "lat": f.lat,
                    "lon": f.lon,
                    "capacity": capacity,
                    "available": max(0, capacity - occupied),
                }
            )
        return _reply(fault, {"facilities": items, "observed_at": world.now.isoformat()})

    return app


def create_weather_app(world: MockWorld, faults: FaultInjector) -> FastAPI:
    app = FastAPI(title="Mock weather (simulated)")
    add_fault_admin(app, faults)

    @app.get("/weather")
    def weather(lat: float, lon: float) -> JSONResponse:
        fault = faults.next("weather.get")
        if fault.mode == "fail":
            return _fail()
        body = {
            "lat": lat,
            "lon": lon,
            "ambient_c": world.ambient_c,
            "wind_kmh": world.wind_kmh,
            "observed_at": world.now.isoformat(),
        }
        return _reply(fault, body)

    return app


def create_congestion_app(world: MockWorld, faults: FaultInjector) -> FastAPI:
    app = FastAPI(title="Mock port congestion (simulated)")
    add_fault_admin(app, faults)

    @app.get("/ports/{port_id}/congestion")
    def congestion(port_id: str) -> JSONResponse:
        fault = faults.next("congestion.get")
        if fault.mode == "fail":
            return _fail()
        if port_id not in world.port_congestion:
            return _reply(fault, {"detail": "unknown_port"}, 404)
        body = {
            "port_id": port_id,
            "level": world.port_congestion[port_id],
            "observed_at": world.now.isoformat(),
        }
        return _reply(fault, body)

    return app


SERVICES = {
    "driver": create_driver_notify_app,
    "ticketing": create_ticketing_app,
    "tms": create_tms_app,
    "facilities": create_facilities_app,
    "weather": create_weather_app,
    "congestion": create_congestion_app,
}


def create_all(world: MockWorld, faults: FaultInjector) -> dict[str, FastAPI]:
    return {name: factory(world, faults) for name, factory in SERVICES.items()}


def create_root_app(world: MockWorld, faults: FaultInjector) -> FastAPI:
    """All six services under one server: /driver, /ticketing, /tms, /facilities, ..."""
    root = FastAPI(title="Cold-chain mock services (simulated)")
    for name, app in create_all(world, faults).items():
        root.mount(f"/{name}", app)
    return root
