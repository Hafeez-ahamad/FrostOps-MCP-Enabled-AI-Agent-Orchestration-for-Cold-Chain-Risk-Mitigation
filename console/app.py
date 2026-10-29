"""Dispatcher console (spec section 12), a Streamlit client of the dispatch API.

Run the API first (`.\\tasks.ps1 api`), then `.\\tasks.ps1 console`, and paste a token it printed.
Screens: incident board, incident detail (temperature timeline against the band), approval
queue, autonomy panel, explainer chat, and a replay stepper for the simulated day.
Everything shown is from a simulated fleet.
"""

import os
import sys
from pathlib import Path
from typing import Any

import altair as alt
import pandas as pd
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from console.client import Api, ApiClient, ApiError  # noqa: E402

PAGES = ["Incident board", "Incident detail", "Approval queue", "Autonomy panel", "Chat", "Replay"]

st.set_page_config(page_title="Cold-Chain Dispatch Console", layout="wide")
st.title("Cold-Chain Dispatch Console")
st.caption("Simulated fleet · policy-bounded autonomy · every action passes the policy gate")


def api() -> Api | None:
    if "api_client" in st.session_state:  # tests inject a fake client here
        client: Api = st.session_state["api_client"]
        return client
    url = st.sidebar.text_input("API URL", os.getenv("CONSOLE_API_URL", "http://127.0.0.1:8000"))
    token = st.sidebar.text_input("Token", os.getenv("CONSOLE_API_TOKEN", ""), type="password")
    return ApiClient(url, token) if token else None


def show(fn: Any) -> Any:
    try:
        return fn()
    except (ApiError, OSError) as exc:
        st.error(f"API error: {exc}")
        return None


client = api()
page = st.sidebar.radio("Screen", PAGES)
if client is None:
    st.info("Enter an API token in the sidebar (the API prints demo tokens when it starts).")
    st.stop()

health = show(lambda: client.get("/health"))
if health:
    cols = st.columns(4)
    cols[0].metric("Simulated time", health["simulated_time"][11:16])
    cols[1].metric("Configuration", health["configuration"])
    cols[2].metric("Kill switch", "ON" if health["kill_switch"] else "off")
    cols[3].metric("Mode", "alert-only" if health["alert_only"] else "normal")

if page == "Incident board":
    incidents = show(lambda: client.get("/incidents")) or []
    open_ones = [i for i in incidents if i["Status"] != "cleared"]
    st.subheader(f"Active incidents ({len(open_ones)}), riskiest first")
    if open_ones:
        frame = pd.DataFrame(open_ones)[
            [
                "IncidentID",
                "ShipmentID",
                "DepotID",
                "CargoType",
                "Status",
                "RuleIds",
                "RiskScore",
                "HitCount",
                "OpenedAt",
            ]
        ]
        st.dataframe(frame, hide_index=True, use_container_width=True)
        st.caption("All incidents are opened only from data-quality-valid readings.")
    else:
        st.write("No active incidents.")

elif page == "Incident detail":
    incidents = show(lambda: client.get("/incidents")) or []
    if incidents:
        chosen = st.selectbox("Incident", [i["IncidentID"] for i in incidents])
        detail = show(lambda: client.get(f"/incidents/{chosen}"))
        if detail:
            st.subheader(f"{detail['IncidentID']} · {detail['ShipmentID']} · {detail['Status']}")
            timeline = pd.DataFrame(detail["timeline"])
            if not timeline.empty:
                timeline["ts"] = pd.to_datetime(timeline["ts"])
                line = alt.Chart(timeline).mark_line().encode(x="ts:T", y="temperature_c:Q")
                layers = [line]
                if detail["band_max_c"] is not None:
                    rule = (
                        alt.Chart(pd.DataFrame({"y": [detail["band_max_c"]]}))
                        .mark_rule(color="red")
                        .encode(y="y:Q")
                    )
                    layers.append(rule)
                st.altair_chart(alt.layer(*layers), use_container_width=True)
                st.caption(
                    "Readings as received, including ones data quality rejected. "
                    "Red: the cargo band's maximum."
                )
            st.markdown("**Rule hits**")
            st.dataframe(pd.DataFrame(detail["rule_hits"]), hide_index=True)
            st.markdown("**Proposals** (planner choice, alternatives, gate outcome)")
            for p in detail["proposals"]:
                st.write(f"- `{p['Action']}` → `{p['Target']}` · {p['Level']} · **{p['Status']}**")
            st.markdown("**Actions**")
            st.dataframe(pd.DataFrame(detail["actions"]), hide_index=True)
            if detail.get("explanation"):
                st.info(detail["explanation"]["summary"])

elif page == "Approval queue":
    pending = show(lambda: client.get("/proposals", status="pending")) or []
    st.subheader(f"Waiting for a human ({len(pending)})")
    for p in pending:
        with st.expander(f"{p['Action']} → {p['Target']} ({p['Level']}) for {p['IncidentID']}"):
            st.json(p["Payload"], expanded=False)
            reason = st.text_input("Reason", key=f"reason-{p['ProposalID']}")
            left, right = st.columns(2)
            body = {"proposal_hash": p["ProposalHash"], "reason": reason}
            if left.button("Approve", key=f"ok-{p['ProposalID']}"):
                result = show(
                    lambda body=body, p=p: client.post(
                        f"/proposals/{p['ProposalID']}/approve", body
                    )
                )
                if result:
                    st.success(f"Gate: {result['outcome']} {result['reasons']}")
            if right.button("Reject", key=f"no-{p['ProposalID']}"):
                result = show(
                    lambda body=body, p=p: client.post(f"/proposals/{p['ProposalID']}/reject", body)
                )
                if result:
                    st.warning(f"Rejected: {result['reasons']}")
    st.caption(
        "Your approval is bound to the exact proposal you see (its hash). "
        "An edited proposal needs a new approval."
    )

elif page == "Autonomy panel":
    if health:
        left, right = st.columns(2)
        if left.button("Kill switch ON" if not health["kill_switch"] else "Kill switch OFF"):
            show(lambda: client.post("/admin/killswitch", {"on": not health["kill_switch"]}))
            st.rerun()
        if right.button("Dry run ON" if not health["dry_run"] else "Dry run OFF"):
            show(lambda: client.post("/admin/dry-run", {"on": not health["dry_run"]}))
            st.rerun()
    policy = show(lambda: client.get("/admin/policy"))
    if policy:
        st.markdown(
            f"**Policy version** `{policy['policy_hash'][:12]}` · "
            f"{len(policy['history'])} stored update(s)"
        )
        st.code(policy["yaml_text"], language="yaml")
    actions = show(lambda: client.get("/actions")) or []
    auto = [a for a in actions if a["level"] == "L2"]
    st.markdown(f"**Automatic (L2) actions** ({len(auto)})")
    if auto:
        st.dataframe(
            pd.DataFrame(auto)[["action", "target", "status", "attempts", "created_at"]],
            hide_index=True,
        )

elif page == "Chat":
    if "chat" not in st.session_state:
        st.session_state.chat = []
    for role, text in st.session_state.chat:
        st.chat_message(role).markdown(text)
    if question := st.chat_input("Ask about incidents, telemetry or the SOP"):
        st.session_state.chat.append(("user", question))
        answer = show(
            lambda: client.post(
                "/chat", {"message": question, "thread_id": st.session_state.get("thread")}
            )
        )
        if answer:
            st.session_state.thread = answer["thread_id"]
            st.session_state.chat.append(("assistant", answer["answer"]))
        st.rerun()

elif page == "Replay":
    st.write("Step the simulated day forward. Readings, incidents, proposals and actions update.")
    cols = st.columns(3)
    for col, minutes in zip(cols, (5, 30, 120), strict=True):
        if col.button(f"+{minutes} min"):
            result = show(
                lambda minutes=minutes: client.post("/admin/advance", {"minutes": minutes})
            )
            if result:
                st.success(f"Simulated time {result['simulated_time']}")
