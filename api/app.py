"""FastAPI console and admin API (spec section 11). Every route checks the caller's role.

GET  /health, /metrics                       no auth (health and Prometheus scrape)
GET  /incidents, /incidents/{id}             read (dispatchers: their depots only)
GET  /proposals?status=pending               read: the approval queue
POST /proposals/{id}/approve | /reject       decide: bound to the proposal hash the human saw
GET  /actions, POST /actions/{id}/cancel     read / cancel
POST /admin/killswitch, /admin/dry-run       control (supervisor, admin)
POST /admin/alert-only/clear                 control: leave alert-only mode after a fix
GET  /admin/policy, PUT /admin/policy        read / policy_write (versioned, safety-checked)
POST /admin/advance                          clock: step the simulated day (demo and replay view)
POST /chat                                   chat with the explainer (read-only tools)
GET  /audit                                  audit export (auditor, supervisor, admin)
"""

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from api.auth import User, UserDirectory
from api.service import EngineService
from engine.actions.models import Approval
from engine.audit.log import verify_chain
from engine.executor.executor import ExecutionRefused
from engine.gate.policy_admin import PolicyRejected
from engine.observability import APPROVALS, log_event, metrics_text

ChatFn = Callable[[str, str, User], str]


class Decision(BaseModel):
    proposal_hash: str = Field(min_length=64, max_length=64)
    reason: str = Field(default="", max_length=400)


class Toggle(BaseModel):
    on: bool


class PolicyUpdate(BaseModel):
    yaml_text: str = Field(min_length=10, max_length=50_000)
    reason: str = Field(min_length=3, max_length=400)


class Advance(BaseModel):
    minutes: float = Field(gt=0, le=24 * 60)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    thread_id: str | None = None


def create_app(service: EngineService, users: UserDirectory, chat: ChatFn | None = None) -> FastAPI:
    app = FastAPI(title="Cold-chain dispatch engine (simulated fleet)", version="0.1.0")
    bearer = HTTPBearer(auto_error=False)

    def current_user(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> User:
        user = users.authenticate(credentials.credentials) if credentials else None
        if user is None:
            raise HTTPException(401, "missing or invalid token")
        return user

    UserDep = Annotated[User, Depends(current_user)]

    def need(user: User, permission: str) -> None:
        if not user.can(permission):
            raise HTTPException(403, f"role {user.role} may not {permission}")

    def visible(user: User, depot: str | None) -> bool:
        return user.sees_depot(depot)

    # --- health and metrics ---------------------------------------------------------------

    @app.get("/health")
    def health() -> dict[str, Any]:
        gate = service.runtime.gate
        return {
            "status": "degraded" if service.runtime.alert_only else "ok",
            "alert_only": service.runtime.alert_only,
            "simulated_time": service.now.isoformat(),
            "kill_switch": gate.controls.is_on("kill_switch"),
            "dry_run": gate.controls.is_on("dry_run"),
            "configuration": service.runtime.config,
            "alert_reason": service.runtime.alert_reason,
            "policy_version": service.runtime.catalog.version,
        }

    @app.get("/metrics")
    def metrics() -> Response:
        return Response(metrics_text(), media_type="text/plain; version=0.0.4")

    # --- incidents ----------------------------------------------------------------------

    @app.get("/incidents")
    def list_incidents(user: UserDep, status: str | None = None) -> list[dict[str, Any]]:
        need(user, "read")
        return [
            i
            for i in service.incidents()
            if visible(user, i["DepotID"]) and (status is None or i["Status"] == status)
        ]

    @app.get("/incidents/{incident_id}")
    def get_incident(incident_id: str, user: UserDep) -> dict[str, Any]:
        need(user, "read")
        incident = service.incident(incident_id)
        if incident is None or not visible(user, incident["DepotID"]):
            raise HTTPException(404, "no such incident")
        return incident

    # --- approval queue -----------------------------------------------------------------

    @app.get("/proposals")
    def list_proposals(user: UserDep, status: str | None = None) -> list[dict[str, Any]]:
        need(user, "read")
        wanted = "awaiting_approval" if status == "pending" else status
        return [p for p in service.proposals(wanted) if visible(user, p["DepotID"])]

    def _decide(proposal_id: str, body: Decision, user: User, decision: str) -> dict[str, Any]:
        need(user, "decide")
        rows = [p for p in service.proposals(None) if p["ProposalID"] == proposal_id]
        if not rows or not visible(user, rows[0]["DepotID"]):
            raise HTTPException(404, "no such proposal")
        approval = Approval(
            approval_id=f"APR-{uuid.uuid4().hex[:16]}",
            proposal_id=proposal_id,
            proposal_hash=body.proposal_hash,
            approver_id=user.user_id,
            approver_role=user.role,
            decision=decision,  # type: ignore[arg-type]
            reason=body.reason,
            decided_at=service.now,
        )
        result = service.decide(approval)
        APPROVALS.labels(decision=decision).inc()
        log_event(
            "api",
            f"proposal {decision}",
            correlation_id=f"COR-{rows[0]['IncidentID']}",
            user=user.user_id,
            outcome=result.outcome.value,
        )
        return result.model_dump(mode="json")

    @app.post("/proposals/{proposal_id}/approve")
    def approve(proposal_id: str, body: Decision, user: UserDep) -> dict[str, Any]:
        return _decide(proposal_id, body, user, "approve")

    @app.post("/proposals/{proposal_id}/reject")
    def reject(proposal_id: str, body: Decision, user: UserDep) -> dict[str, Any]:
        return _decide(proposal_id, body, user, "reject")

    # --- actions ----------------------------------------------------------------------------

    @app.get("/actions")
    def list_actions(user: UserDep) -> list[dict[str, Any]]:
        need(user, "read")
        incidents = {i["IncidentID"]: i["DepotID"] for i in service.incidents()}
        return [
            a.model_dump(mode="json")
            for a in service.runtime.executor.actions.all()
            if visible(user, incidents.get(a.incident_id))
        ]

    @app.post("/actions/{action_id}/cancel")
    def cancel(action_id: str, user: UserDep) -> dict[str, Any]:
        need(user, "cancel")
        try:
            return service.cancel(action_id, f"{user.role}:{user.user_id}")
        except ExecutionRefused as exc:
            raise HTTPException(409, str(exc)) from exc

    # --- supervisor controls and policy -----------------------------------------------------------

    @app.post("/admin/killswitch")
    def killswitch(body: Toggle, user: UserDep) -> dict[str, Any]:
        need(user, "control")
        service.set_control("kill_switch", body.on, f"{user.role}:{user.user_id}")
        return {"kill_switch": body.on}

    @app.post("/admin/dry-run")
    def dry_run(body: Toggle, user: UserDep) -> dict[str, Any]:
        need(user, "control")
        service.set_control("dry_run", body.on, f"{user.role}:{user.user_id}")
        return {"dry_run": body.on}

    @app.post("/admin/alert-only/clear")
    def clear_alert_only(user: UserDep) -> dict[str, Any]:
        need(user, "control")
        service.clear_alert_only(f"{user.role}:{user.user_id}")
        return {"alert_only": service.runtime.alert_only}

    @app.get("/admin/policy")
    def get_policy(user: UserDep) -> dict[str, Any]:
        need(user, "read")
        return service.policy.current()

    @app.put("/admin/policy")
    def put_policy(body: PolicyUpdate, user: UserDep) -> dict[str, Any]:
        need(user, "policy_write")
        try:
            return service.update_policy(body.yaml_text, f"{user.role}:{user.user_id}", body.reason)
        except PolicyRejected as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/admin/advance")
    def advance(body: Advance, user: UserDep) -> dict[str, Any]:
        need(user, "clock")
        now: datetime = service.advance(body.minutes)
        return {"simulated_time": now.isoformat()}

    # --- chat and audit ------------------------------------------------------------------

    @app.post("/chat")
    def chat_route(body: ChatRequest, user: UserDep) -> dict[str, Any]:
        need(user, "chat")
        if chat is None:
            raise HTTPException(503, "chat is not configured")
        thread = body.thread_id or f"{user.user_id}-{uuid.uuid4().hex[:8]}"
        try:
            answer = chat(body.message, thread, user)
        except Exception as exc:  # noqa: BLE001 - any model-side failure (connection, config, timeout)
            raise HTTPException(503, f"Chat model unavailable: {exc}") from exc
        return {"thread_id": thread, "answer": answer}

    @app.get("/audit")
    def audit(user: UserDep, event_type: str | None = None, limit: int = 200) -> dict[str, Any]:
        need(user, "audit")
        rows = service.audit(event_type, min(max(limit, 1), 5000))
        chain_ok = verify_chain(service.audit(None, 1_000_000)[::-1])
        return {"chain_valid": chain_ok, "entries": rows}

    return app
