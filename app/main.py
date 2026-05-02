from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.config import settings
from app.engine import classify_reply_intent, is_probable_auto_reply, pick_actions
from app.schemas import (
    ContextPushRequest,
    ContextPushResponse,
    HealthzResponse,
    MetadataResponse,
    ReplyRequest,
    ReplyResponse,
    TickRequest,
    TickResponse,
)
from app.store import InMemoryStore


app = FastAPI(title="Vera Challenge Bot", version=settings.version)
store = InMemoryStore()
started_at = datetime.now(timezone.utc)


@app.get("/v1/healthz", response_model=HealthzResponse)
def healthz() -> HealthzResponse:
    uptime = int((datetime.now(timezone.utc) - started_at).total_seconds())
    return HealthzResponse(status="ok", uptime_seconds=uptime, contexts_loaded=store.count_contexts())


@app.get("/v1/metadata", response_model=MetadataResponse)
def metadata() -> MetadataResponse:
    return MetadataResponse(
        team_name=settings.team_name,
        team_members=[m.strip() for m in settings.team_members.split(",") if m.strip()],
        model=settings.model,
        approach=settings.approach,
        contact_email=settings.contact_email,
        version=settings.version,
        submitted_at=datetime.now(timezone.utc).isoformat(),
    )


@app.post("/v1/context", response_model=ContextPushResponse)
def push_context(req: ContextPushRequest) -> JSONResponse:
    result = store.put_context(
        scope=req.scope,
        context_id=req.context_id,
        version=req.version,
        payload=req.payload,
        delivered_at=req.delivered_at,
    )
    if not result.accepted and result.reason == "stale_version":
        return JSONResponse(
            status_code=409,
            content=ContextPushResponse(
                accepted=False, reason="stale_version", current_version=result.current_version
            ).model_dump(mode="json"),
        )

    return JSONResponse(
        status_code=200,
        content=ContextPushResponse(
            accepted=True, ack_id=result.ack_id, stored_at=result.stored_at
        ).model_dump(mode="json"),
    )


@app.post("/v1/tick", response_model=TickResponse)
def tick(req: TickRequest) -> TickResponse:
    categories = store.contexts_by_scope("category")
    merchants = store.contexts_by_scope("merchant")
    customers = store.contexts_by_scope("customer")
    triggers = store.contexts_by_scope("trigger")

    suppressed_keys = store.suppression_keys()
    actions = pick_actions(
        now=req.now,
        available_trigger_ids=req.available_triggers,
        triggers=triggers,
        merchants=merchants,
        categories=categories,
        customers=customers,
        suppressed_keys=suppressed_keys,
        max_actions=20,
    )

    # Mark used suppression keys
    for a in actions:
        store.mark_suppressed(a.suppression_key, req.now)
        store.upsert_conversation(
            a.conversation_id,
            {
                "conversation_id": a.conversation_id,
                "merchant_id": a.merchant_id,
                "customer_id": a.customer_id,
                "trigger_id": a.trigger_id,
                "last_body": a.body,
                "last_sent_at": req.now.isoformat(),
            },
        )

    return TickResponse(actions=[x.__dict__ for x in actions])  # pydantic will validate


@app.post("/v1/reply", response_model=ReplyResponse)
def reply(req: ReplyRequest) -> ReplyResponse:
    conv = store.get_conversation(req.conversation_id) or {}
    intent = classify_reply_intent(req.message)

    if is_probable_auto_reply(req.message):
        auto_count = int(conv.get("auto_reply_count") or 0) + 1
        conv["auto_reply_count"] = auto_count
        store.upsert_conversation(req.conversation_id, conv)
        if auto_count >= 2:
            return {
                "action": "end",
                "rationale": "Repeated auto-reply detected; ending thread to avoid spam loop.",
            }
        return {
            "action": "wait",
            "wait_seconds": 900,
            "rationale": "Detected likely WhatsApp auto-reply; short backoff before one final retry.",
        }

    if intent == "stop":
        return {"action": "end", "rationale": "User opted out / asked to stop."}

    if intent == "defer":
        return {"action": "wait", "wait_seconds": 1800, "rationale": "User asked to revisit later; backing off 30 minutes."}

    if intent == "accept":
        merchant = store.get_context("merchant", req.merchant_id)
        customer = store.get_context("customer", req.customer_id) if req.customer_id else None
        owner_first = ""
        merchant_name = req.merchant_id
        customer_name = ""
        if merchant:
            owner_first = str(((merchant.payload.get("identity") or {}).get("owner_first_name")) or "")
            merchant_name = str(((merchant.payload.get("identity") or {}).get("name")) or req.merchant_id)
        if customer:
            customer_name = str(((customer.payload.get("identity") or {}).get("name")) or "")
        trigger_id = str(conv.get("trigger_id") or "")
        # Route customer replies to customer-safe confirmation instead of merchant-facing ack.
        if req.from_role == "customer" or req.customer_id:
            name_prefix = f"{customer_name}, " if customer_name else ""
            return {
                "action": "send",
                "body": f"Done {name_prefix}booking request noted with {merchant_name}. We will confirm slot details shortly.",
                "cta": "none",
                "rationale": "Customer accept routed to booking confirmation using customer context.",
            }

        prefix = f"Got it {owner_first}".strip() if owner_first else "Got it"
        if "regulation_change" in trigger_id or "compliance" in trigger_id or "xray" in req.message.lower():
            return {
                "action": "send",
                "body": "Noted. For old D-speed units: 1) verify current dose calibration, 2) migrate to E-speed/RVG workflow, 3) log SOP update before deadline. Want a printable checklist format?",
                "cta": "yes_no",
                "rationale": "Compliance-focused merchant reply gets concrete next steps instead of generic clarification.",
            }
        return {
            "action": "send",
            "body": f"{prefix} — done. I’ll prepare the draft for {trigger_id or 'this signal'} now and send it in one message.",
            "cta": "ack",
            "rationale": "Accepted intent detected; proceeding immediately without adding extra friction.",
        }

    # Unknown: keep it low-friction, avoid multiple asks
    if req.from_role == "customer" or req.customer_id:
        return {
            "action": "send",
            "body": "Thanks — do you want me to proceed with this booking/request now? Reply yes or no.",
            "cta": "yes_no",
            "rationale": "Customer intent unclear; asking one explicit yes/no confirmation.",
        }
    return {
        "action": "send",
        "body": "Quick yes/no so I can proceed — should I go ahead?",
        "cta": "yes_no",
        "rationale": "Reply intent unclear; asking a single low-effort clarification.",
    }

