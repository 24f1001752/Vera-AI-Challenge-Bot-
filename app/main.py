from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Dict, List, Optional

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


def _extract_requested_slot(message: str, offered_slots: List[str]) -> Optional[str]:
    t = (message or "").strip()
    if not t:
        return None
    if offered_slots:
        compact = t.lower()
        if re.search(r"\b1\b|first", compact):
            return offered_slots[0]
        if len(offered_slots) > 1 and re.search(r"\b2\b|second", compact):
            return offered_slots[1]
    cleaned = " ".join(t.split())
    # Free-text slot mention (e.g. "Wed 5 Nov, 6pm")
    if re.search(r"\b(mon|tue|wed|thu|fri|sat|sun|\d{1,2}\s?(am|pm)|\d{1,2}[:.]\d{2})\b", cleaned, re.IGNORECASE):
        return cleaned
    return None


def _is_off_topic_merchant_ask(message: str) -> bool:
    t = (message or "").lower()
    return any(
        k in t
        for k in [
            "gst",
            "tax filing",
            "itr",
            "income tax",
            "loan",
            "visa",
            "passport",
            "weather",
            "cricket score",
        ]
    )


def _merchant_accept_followup(conv: Dict[str, object], req: ReplyRequest) -> Optional[Dict[str, str]]:
    trigger_id = str(conv.get("trigger_id") or "")
    trigger = store.get_context("trigger", trigger_id)
    trigger_payload = (trigger.payload.get("payload") if trigger and isinstance(trigger.payload.get("payload"), dict) else {}) or {}
    trigger_kind = str((trigger.payload.get("kind") if trigger else "") or "")
    merchant = store.get_context("merchant", req.merchant_id)
    merchant_payload = merchant.payload if merchant else {}
    merchant_name = str(((merchant_payload.get("identity") or {}).get("name")) or req.merchant_id)
    active_offer = ""
    offers = merchant_payload.get("offers") or []
    if isinstance(offers, list):
        active = next((o for o in offers if isinstance(o, dict) and str(o.get("status")) == "active"), None)
        if isinstance(active, dict):
            active_offer = str(active.get("title") or "")

    if trigger_kind == "research_digest":
        return {
            "action": "send",
            "body": f"Great. I will send a 3-bullet abstract summary and one patient-facing WhatsApp draft now for {merchant_name}. {active_offer and ('I will align it with your active offer: ' + active_offer + '.')}",
            "cta": "yes_no",
            "rationale": "Merchant accepted research assist; switching immediately to concrete deliverables.",
        }
    if trigger_kind == "perf_dip":
        metric = str(trigger_payload.get("metric") or "CTR")
        return {
            "action": "send",
            "body": f"Understood. I will draft one recovery message focused on {metric} plus a single offer-led post you can publish this week.",
            "cta": "yes_no",
            "rationale": "Intent transition handled by moving directly from diagnosis to execution.",
        }
    if trigger_kind == "competitor_opened":
        competitor = str(trigger_payload.get("competitor_name") or "nearby competitor")
        return {
            "action": "send",
            "body": f"Done. I will prepare a defensive outreach draft now so {competitor} does not pull nearby demand this week.",
            "cta": "yes_no",
            "rationale": "Accepted competitor alert converted into immediate defensive action.",
        }
    if trigger_kind == "renewal_due":
        days = trigger_payload.get("days_remaining")
        return {
            "action": "send",
            "body": f"Perfect. I will send a quick ROI summary and renewal recommendation for the next {days} days window.",
            "cta": "yes_no",
            "rationale": "Accepted renewal discussion moved to concrete ROI artifact.",
        }
    return None


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
        trigger = triggers.get(a.trigger_id) or {}
        payload = trigger.get("payload") if isinstance(trigger.get("payload"), dict) else {}
        payload = payload or {}
        raw_slots = payload.get("available_slots") if isinstance(payload.get("available_slots"), list) else []
        offered_slots = [
            str(s.get("label"))
            for s in raw_slots
            if isinstance(s, dict) and s.get("label")
        ]
        store.upsert_conversation(
            a.conversation_id,
            {
                "conversation_id": a.conversation_id,
                "merchant_id": a.merchant_id,
                "customer_id": a.customer_id,
                "trigger_id": a.trigger_id,
                "last_body": a.body,
                "last_cta": a.cta,
                "offered_slots": offered_slots,
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

    # Keep off-topic handling explicit for replay scenarios.
    if req.from_role == "merchant" and _is_off_topic_merchant_ask(req.message):
        return {
            "action": "send",
            "body": "I cannot help with that directly, but I can continue with your current growth task here. Should I proceed with the draft now?",
            "cta": "yes_no",
            "rationale": "Out-of-scope merchant query redirected politely back to the active conversation objective.",
        }

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
        if req.from_role == "customer":
            offered_slots = conv.get("offered_slots") if isinstance(conv.get("offered_slots"), list) else []
            offered_slots = [str(x) for x in offered_slots if isinstance(x, str)]
            chosen_slot = _extract_requested_slot(req.message, offered_slots)
            name_prefix = f"{customer_name}, " if customer_name else ""
            slot_suffix = f" for {chosen_slot}" if chosen_slot else ""
            return {
                "action": "send",
                "body": f"Done {name_prefix}booking request noted with {merchant_name}{slot_suffix}. We will confirm shortly.",
                "cta": "none",
                "rationale": "Customer accept routed to booking confirmation with slot grounding when provided.",
            }

        merchant_followup = _merchant_accept_followup(conv, req)
        if merchant_followup:
            return merchant_followup

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
    if req.from_role == "customer":
        offered_slots = conv.get("offered_slots") if isinstance(conv.get("offered_slots"), list) else []
        offered_slots = [str(x) for x in offered_slots if isinstance(x, str)]
        chosen_slot = _extract_requested_slot(req.message, offered_slots)
        if chosen_slot:
            customer = store.get_context("customer", req.customer_id) if req.customer_id else None
            customer_name = ""
            if customer:
                customer_name = str(((customer.payload.get("identity") or {}).get("name")) or "")
            name_prefix = f"{customer_name}, " if customer_name else ""
            merchant = store.get_context("merchant", req.merchant_id)
            merchant_name = req.merchant_id
            if merchant:
                merchant_name = str(((merchant.payload.get("identity") or {}).get("name")) or req.merchant_id)
            return {
                "action": "send",
                "body": f"Perfect {name_prefix}slot request noted with {merchant_name} for {chosen_slot}. We will confirm shortly.",
                "cta": "none",
                "rationale": "Customer shared a concrete slot; confirming directly avoids an unnecessary clarification loop.",
            }
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

