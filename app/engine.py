from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


def _safe_get(d: Dict[str, Any], path: List[str], default: Any = None) -> Any:
    cur: Any = d
    for p in path:
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur


def _iso_now(now: datetime) -> datetime:
    return now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)


def _normalize_text(s: str) -> str:
    return " ".join((s or "").strip().lower().split())


def _to_datetime(v: Any) -> Optional[datetime]:
    if not isinstance(v, str):
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None


def _pct(v: Any) -> str:
    if isinstance(v, (int, float)):
        return f"{int(round(v * 100))}%"
    return ""


def _one_of(items: List[str]) -> str:
    return ", ".join(i for i in items if i)


def _derive_send_as(scope: str) -> str:
    return "vera" if scope == "merchant" else "merchant_on_behalf"


def _find_active_offer(merchant: Dict[str, Any]) -> str:
    offers = merchant.get("offers") or []
    if not isinstance(offers, list):
        return ""
    active = next((o for o in offers if isinstance(o, dict) and str(o.get("status")) == "active"), None)
    if not isinstance(active, dict):
        return ""
    return str(active.get("title") or "")


def _category_prefix(category_slug: str, owner_first: str) -> str:
    if category_slug == "dentists" and owner_first:
        return f"Dr. {owner_first}"
    if owner_first:
        return owner_first
    return "Hi"


def _fact_line(merchant: Dict[str, Any], category: Dict[str, Any]) -> str:
    views = _safe_get(merchant, ["performance", "views"])
    calls = _safe_get(merchant, ["performance", "calls"])
    ctr = _safe_get(merchant, ["performance", "ctr"])
    peer_ctr = _safe_get(category, ["peer_stats", "avg_ctr"])
    parts: List[str] = []
    if isinstance(views, int):
        parts.append(f"{views} views")
    if isinstance(calls, int):
        parts.append(f"{calls} calls")
    if isinstance(ctr, (int, float)):
        parts.append(f"CTR {ctr:.3f}")
    if isinstance(peer_ctr, (int, float)):
        parts.append(f"peer CTR {peer_ctr:.3f}")
    return _one_of(parts)


def is_probable_auto_reply(text: str) -> bool:
    t = _normalize_text(text)
    patterns = [
        "thank you for contacting",
        "we will get back",
        "our team will contact",
        "auto reply",
        "automated message",
        "currently unavailable",
        "please wait",
    ]
    return any(p in t for p in patterns)


def classify_reply_intent(text: str) -> str:
    """
    Deterministic, lightweight intent classifier for /v1/reply.
    Returns: "accept" | "defer" | "stop" | "unknown"
    """
    t = _normalize_text(text)
    if any(k in t for k in ["stop", "unsubscribe", "dont message", "don't message", "do not message", "opt out", "remove me", "shut up", "go away", "leave me alone"]):
        return "stop"
    if any(k in t for k in ["later", "tomorrow", "next week", "remind", "busy", "after"]):
        return "defer"
    if any(k in t for k in ["yes", "ok", "okay", "sure", "send", "go ahead", "please do", "haan", "haa", "done"]):
        return "accept"
    return "unknown"


@dataclass(frozen=True)
class ComposedAction:
    conversation_id: str
    merchant_id: str
    customer_id: Optional[str]
    send_as: str
    trigger_id: str
    template_name: str
    template_params: List[str]
    body: str
    cta: str
    suppression_key: str
    rationale: str


def _base_compose(
    *,
    tid: str,
    trigger: Dict[str, Any],
    merchant: Dict[str, Any],
    body: str,
    template_name: str,
    template_params: List[str],
    cta: str,
    rationale: str,
) -> ComposedAction:
    kind = str(trigger.get("kind") or "")
    scope = str(trigger.get("scope") or "")
    merchant_id = str(trigger.get("merchant_id") or "")
    customer_id = trigger.get("customer_id")
    customer_id = str(customer_id) if customer_id is not None else None
    suppression_key = str(trigger.get("suppression_key") or f"{kind}:{merchant_id}")
    return ComposedAction(
        conversation_id=f"conv_{tid}",
        merchant_id=merchant_id,
        customer_id=customer_id,
        send_as=_derive_send_as(scope),
        trigger_id=tid,
        template_name=template_name,
        template_params=template_params,
        body=body.strip(),
        cta=cta,
        suppression_key=suppression_key,
        rationale=rationale,
    )


def pick_actions(
    *,
    now: datetime,
    available_trigger_ids: List[str],
    triggers: Dict[str, Dict[str, Any]],
    merchants: Dict[str, Dict[str, Any]],
    categories: Dict[str, Dict[str, Any]],
    customers: Dict[str, Dict[str, Any]],
    suppressed_keys: set[str],
    max_actions: int = 20,
) -> List[ComposedAction]:
    now = _iso_now(now)

    # Candidate trigger objects
    candidates: List[Dict[str, Any]] = []
    for tid in sorted(set(available_trigger_ids)):
        trg = triggers.get(tid)
        if not trg:
            continue
        exp = trg.get("expires_at")
        if isinstance(exp, str):
            try:
                exp_dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
            except ValueError:
                exp_dt = None
            if exp_dt is not None and exp_dt < now:
                continue
        sup_key = str(trg.get("suppression_key") or "")
        if sup_key and sup_key in suppressed_keys:
            continue
        candidates.append(trg)

    def score_trigger(trg: Dict[str, Any]) -> Tuple[int, int, int, str]:
        # Higher is better. Stable tie-break with trigger id.
        urgency = int(trg.get("urgency") or 0)
        kind = str(trg.get("kind") or "")
        scope = str(trg.get("scope") or "")
        merchant_id = str(trg.get("merchant_id") or "")
        expires_at = _to_datetime(trg.get("expires_at"))

        m = merchants.get(merchant_id, {})
        cat_slug = str(m.get("category_slug") or _safe_get(trg, ["payload", "category"], "")) or ""
        cat = categories.get(cat_slug, {})

        merchant_fit = 0
        if m:
            signals = m.get("signals") or []
            if isinstance(signals, list):
                if kind in ["perf_dip", "seasonal_perf_dip"] and any("perf_dip" in s or "ctr_below" in s for s in signals):
                    merchant_fit += 2
                if kind == "research_digest" and any("high_risk" in s for s in signals):
                    merchant_fit += 2
                if kind == "review_theme_emerged" and m.get("review_themes"):
                    merchant_fit += 1
        category_fit = 1 if cat else 0

        scope_bonus = 1 if scope in ["merchant", "customer"] else 0
        expiry_boost = 0
        if expires_at is not None:
            secs = int((expires_at - now).total_seconds())
            if secs <= 6 * 3600:
                expiry_boost = 2
            elif secs <= 24 * 3600:
                expiry_boost = 1
        kind_bonus = 1 if kind in ["active_planning_intent", "recall_due", "renewal_due", "regulation_change"] else 0
        return (urgency + kind_bonus, merchant_fit + category_fit + scope_bonus + expiry_boost, 0, str(trg.get("id") or ""))

    # Sort by score descending with stable tie-breakers
    candidates.sort(key=lambda t: score_trigger(t), reverse=True)

    actions: List[ComposedAction] = []
    for trg in candidates:
        if len(actions) >= max_actions:
            break
        action = compose_action(
            now=now,
            trigger=trg,
            merchants=merchants,
            categories=categories,
            customers=customers,
        )
        if action:
            actions.append(action)
    return actions


def compose_action(
    *,
    now: datetime,
    trigger: Dict[str, Any],
    merchants: Dict[str, Dict[str, Any]],
    categories: Dict[str, Dict[str, Any]],
    customers: Dict[str, Dict[str, Any]],
) -> Optional[ComposedAction]:
    tid = str(trigger.get("id") or "")
    kind = str(trigger.get("kind") or "")
    scope = str(trigger.get("scope") or "")
    merchant_id = str(trigger.get("merchant_id") or "")
    customer_id = trigger.get("customer_id")
    customer_id = str(customer_id) if customer_id is not None else None

    merchant = merchants.get(merchant_id)
    if not merchant:
        return None
    cat_slug = str(merchant.get("category_slug") or "")
    category = categories.get(cat_slug, {})

    owner_first = str(_safe_get(merchant, ["identity", "owner_first_name"], ""))
    merchant_name = str(_safe_get(merchant, ["identity", "name"], merchant_id))
    city = str(_safe_get(merchant, ["identity", "city"], ""))
    locality = str(_safe_get(merchant, ["identity", "locality"], ""))
    category_voice = str(_safe_get(category, ["voice", "tone"], ""))
    offer_txt = _find_active_offer(merchant)
    fact = _fact_line(merchant, category)
    prefix = _category_prefix(cat_slug, owner_first)
    payload = trigger.get("payload") if isinstance(trigger.get("payload"), dict) else {}
    payload = payload or {}

    # Message families: deterministic templates for the most common trigger kinds.
    if kind == "research_digest" and scope == "merchant":
        top_item_id = str(payload.get("top_item_id") or "")
        digest_items = category.get("digest") or []
        item = next((x for x in digest_items if str(x.get("id")) == top_item_id), None) or (digest_items[0] if digest_items else None)
        if not item:
            title = "Weekly research digest update"
            source = "category_digest"
            summary = "One new practical item is relevant for your current patient/customer mix."
            body = (
                f"{prefix}, {title}. {summary} "
                "Want me to send a concise 3-bullet brief you can use today?"
            )
            return _base_compose(
                tid=tid,
                trigger=trigger,
                merchant=merchant,
                body=body,
                template_name="vera_research_digest_v1",
                template_params=[owner_first, source],
                cta="yes_no",
                rationale="Research digest trigger fallback with one practical next step.",
            )
        title = str(item.get("title") or "New research update")
        source = str(item.get("source") or "")
        trial_n = item.get("trial_n")
        summary = str(item.get("summary") or "")

        high_risk = _safe_get(merchant, ["customer_aggregate", "high_risk_adult_count"])
        anchor = f"relevant to your high-risk adult patients" if high_risk else "relevant to your case-mix"
        n_txt = f"{trial_n:,}-patient" if isinstance(trial_n, int) else ""

        body = " ".join(
            p
            for p in [
                f"{prefix}, {title}.",
                f"One item {anchor} — {n_txt} trial: {summary}" if n_txt else f"One item {anchor}: {summary}",
                f"Worth a quick look. Want me to pull the key points + draft a patient-friendly WhatsApp you can share? — {source}".strip(),
            ]
            if p
        )
        return _base_compose(
            tid=tid,
            trigger=trigger,
            merchant=merchant,
            body=body,
            template_name="vera_research_digest_v1",
            template_params=[owner_first, source],
            cta="yes_no",
            rationale="Research digest trigger with category-cited source; anchored to merchant segment and asks one low-effort yes/no.",
        )

    if kind == "regulation_change" and scope == "merchant":
        top_item_id = str(payload.get("top_item_id") or "")
        deadline = str(payload.get("deadline_iso") or "")
        digest_items = category.get("digest") or []
        item = next((x for x in digest_items if str(x.get("id")) == top_item_id), None)
        title = str((item or {}).get("title") or "Compliance update")
        source = str((item or {}).get("source") or "")
        actionable = str((item or {}).get("actionable") or "Review SOP before deadline.")
        body = (
            f"{prefix}, compliance update: {title}. Deadline: {deadline}. "
            f"{actionable} {source and ('Source: ' + source + '.')} "
            "Want a 3-step checklist tailored to your clinic/store?"
        )
        return _base_compose(
            tid=tid,
            trigger=trigger,
            merchant=merchant,
            body=body,
            template_name="vera_regulation_change_v1",
            template_params=[owner_first, deadline],
            cta="yes_no",
            rationale="Regulation trigger prioritized by deadline with one actionable compliance CTA.",
        )

    if kind in ["perf_dip", "seasonal_perf_dip"] and scope == "merchant":
        metric = str(payload.get("metric") or "metric")
        delta_pct = payload.get("delta_pct")
        window = str(payload.get("window") or "7d")
        pct_txt = f"{int(delta_pct*100)}%" if isinstance(delta_pct, (int, float)) else ""
        peer_ctr = _safe_get(category, ["peer_stats", "avg_ctr"])
        cur_ctr = _safe_get(merchant, ["performance", "ctr"])
        hint = ""
        if metric == "ctr" and isinstance(cur_ctr, (int, float)) and isinstance(peer_ctr, (int, float)):
            hint = f"Peer avg CTR is {peer_ctr:.3f} vs yours {cur_ctr:.3f}."

        body = (
            f"{prefix}, quick heads-up: your {metric} is down {pct_txt} over {window}. "
            f"{hint} Want me to draft one grounded offer+post you can push this week (single message, ready to send)?"
        ).strip()
        return _base_compose(
            tid=tid,
            trigger=trigger,
            merchant=merchant,
            body=body,
            template_name="vera_perf_dip_v1",
            template_params=[owner_first, metric, pct_txt],
            cta="yes_no",
            rationale="Performance dip trigger; grounded to metric and deltas; proposes a single next step with yes/no CTA.",
        )

    if kind == "perf_spike" and scope == "merchant":
        metric = str(payload.get("metric") or "metric")
        delta_pct = _pct(payload.get("delta_pct"))
        body = (
            f"{prefix}, strong signal: your {metric} is up {delta_pct} this week in {locality}. "
            f"{offer_txt and ('Current offer: ' + offer_txt + '. ')}"
            "Should I draft a quick follow-up message to convert this spike while demand is hot?"
        )
        return _base_compose(
            tid=tid,
            trigger=trigger,
            merchant=merchant,
            body=body,
            template_name="vera_perf_spike_v1",
            template_params=[owner_first, metric, delta_pct],
            cta="yes_no",
            rationale="Performance spike trigger converted into immediate conversion action with one yes/no CTA.",
        )

    if kind == "ipl_match_today" and scope == "merchant" and cat_slug == "restaurants":
        match = str(payload.get("match") or "IPL match")
        venue = str(payload.get("venue") or "")
        is_weeknight = payload.get("is_weeknight")
        nuance = "Weeknight matches usually lift covers; Saturday often shifts to home-watch parties." if is_weeknight is False else "Match nights can swing footfall — worth timing a quick push."
        body = (
            f"Quick heads-up {prefix} — {match} at {venue} today. {nuance} "
            f"{('Your active offer: ' + offer_txt + '. ') if offer_txt else ''}"
            "Want me to draft a single WhatsApp promo message for today (one-line CTA)?"
        ).strip()
        return _base_compose(
            tid=tid,
            trigger=trigger,
            merchant=merchant,
            body=body,
            template_name="vera_ipl_prompt_v1",
            template_params=[owner_first, match, locality, city],
            cta="yes_no",
            rationale="Restaurant IPL trigger; grounded to match details and existing active offer; asks for one yes/no to draft a promo.",
        )

    if kind in ["festival_upcoming", "competitor_opened", "milestone_reached", "review_theme_emerged", "curious_ask_due", "dormant_with_vera", "winback_eligible", "renewal_due", "active_planning_intent"] and scope == "merchant":
        if kind == "renewal_due":
            days_remaining = payload.get("days_remaining")
            amount = payload.get("renewal_amount")
            body = (
                f"{prefix}, your plan renewal is due in {days_remaining} days (₹{amount}). "
                f"{fact and ('Current snapshot: ' + fact + '. ')}"
                "Want me to prepare a quick ROI summary you can review before renewing?"
            )
            return _base_compose(
                tid=tid,
                trigger=trigger,
                merchant=merchant,
                body=body,
                template_name="vera_renewal_due_v1",
                template_params=[owner_first, str(days_remaining), str(amount)],
                cta="yes_no",
                rationale="Renewal trigger tied to days-remaining and amount; one clear review CTA.",
            )

        if kind == "active_planning_intent":
            topic = str(payload.get("intent_topic") or "campaign planning")
            last_msg = str(payload.get("merchant_last_message") or "")
            body = (
                f"{prefix}, got it on {topic}. "
                f"{last_msg and ('You said: ' + last_msg + '. ')}"
                "Should I send a ready 3-point draft you can copy as-is?"
            )
            return _base_compose(
                tid=tid,
                trigger=trigger,
                merchant=merchant,
                body=body,
                template_name="vera_active_planning_v1",
                template_params=[owner_first, topic],
                cta="yes_no",
                rationale="Planning-intent handoff: converts merchant interest directly into a concrete draft proposal.",
            )

        if kind == "review_theme_emerged":
            theme = str(payload.get("theme") or "service quality")
            occ = payload.get("occurrences_30d")
            quote = str(payload.get("common_quote") or "")
            body = (
                f"{prefix}, review trend spotted: '{theme}' came up {occ} times in 30 days. "
                f"{quote and ('Sample quote: ' + quote + '. ')}"
                "Want a one-line response template plus one corrective post?"
            )
            return _base_compose(
                tid=tid,
                trigger=trigger,
                merchant=merchant,
                body=body,
                template_name="vera_review_theme_v1",
                template_params=[owner_first, theme, str(occ)],
                cta="yes_no",
                rationale="Review-theme trigger uses exact frequency and asks one corrective action.",
            )

        if kind == "milestone_reached":
            metric = str(payload.get("metric") or "metric")
            value_now = payload.get("value_now")
            milestone = payload.get("milestone_value")
            body = (
                f"{prefix}, milestone watch: {metric} is {value_now} (target {milestone}). "
                "Want me to draft a short message to convert this momentum into more reviews/bookings?"
            )
            return _base_compose(
                tid=tid,
                trigger=trigger,
                merchant=merchant,
                body=body,
                template_name="vera_milestone_v1",
                template_params=[owner_first, metric, str(value_now), str(milestone)],
                cta="yes_no",
                rationale="Milestone trigger grounded to current value vs target with single conversion CTA.",
            )

        if kind == "festival_upcoming":
            festival = str(payload.get("festival") or "festival")
            date = str(payload.get("date") or "")
            body = (
                f"{prefix}, {festival} is coming on {date}. "
                f"{offer_txt and ('You already have: ' + offer_txt + '. ')}"
                "Should I draft one festival message tailored to your category voice?"
            )
            return _base_compose(
                tid=tid,
                trigger=trigger,
                merchant=merchant,
                body=body,
                template_name="vera_festival_v1",
                template_params=[owner_first, festival, date],
                cta="yes_no",
                rationale="Festival trigger with explicit date and one message draft CTA.",
            )

        if kind == "competitor_opened":
            competitor = str(payload.get("competitor_name") or "a nearby competitor")
            distance = payload.get("distance_km")
            body = (
                f"{prefix}, local alert: {competitor} opened nearby ({distance} km). "
                "Want one defensive outreach message using your current offer so you retain nearby demand?"
            )
            return _base_compose(
                tid=tid,
                trigger=trigger,
                merchant=merchant,
                body=body,
                template_name="vera_competitor_alert_v1",
                template_params=[owner_first, competitor, str(distance)],
                cta="yes_no",
                rationale="Competitor trigger converted to an immediate defensive action with one CTA.",
            )

        if kind == "dormant_with_vera":
            days = payload.get("days_since_last_reply") or payload.get("dormant_days")
            body = (
                f"{prefix}, you’ve been quiet with Vera for {days} days. "
                "Can I send one zero-setup idea based on your latest metrics and offers?"
            )
            return _base_compose(
                tid=tid,
                trigger=trigger,
                merchant=merchant,
                body=body,
                template_name="vera_dormant_reengage_v1",
                template_params=[owner_first, str(days)],
                cta="yes_no",
                rationale="Dormancy trigger uses concrete inactivity days and asks permission for one lightweight idea.",
            )

        if kind == "winback_eligible":
            lapsed = payload.get("lapsed_customers_added_since_expiry") or payload.get("lapsed_customers")
            body = (
                f"{prefix}, winback opportunity: {lapsed} customers are now lapsed. "
                "Should I draft one comeback message with a single low-friction yes/no CTA?"
            )
            return _base_compose(
                tid=tid,
                trigger=trigger,
                merchant=merchant,
                body=body,
                template_name="vera_winback_v1",
                template_params=[owner_first, str(lapsed)],
                cta="yes_no",
                rationale="Winback trigger grounded to lapsed customer count and one immediate action.",
            )

        # curious_ask_due and unknown merchant-scope support
        body = (
            f"{prefix}, quick pulse check for {merchant_name} in {locality}: "
            f"{fact}. Should I send one actionable recommendation right now?"
        )
        return _base_compose(
            tid=tid,
            trigger=trigger,
            merchant=merchant,
            body=body,
            template_name="vera_merchant_nudge_v1",
            template_params=[owner_first, merchant_name, city],
            cta="yes_no",
            rationale=f"{kind} trigger mapped to a single actionable merchant nudge with grounded context.",
        )

    if scope == "customer" and kind in [
        "recall_due",
        "appointment_tomorrow",
        "trial_followup",
        "wedding_package_followup",
        "customer_lapsed_soft",
        "customer_lapsed_hard",
        "chronic_refill_due",
        "customer_lapsed",
    ]:
        customer = customers.get(customer_id or "")
        if not customer:
            return None
        consent = _safe_get(customer, ["preferences", "reminder_opt_in"], True)
        if consent is False:
            return None

        cust_name = str(_safe_get(customer, ["identity", "name"], "there"))
        lang = str(_safe_get(customer, ["identity", "language_pref"], ""))

        slots = payload.get("available_slots", [])
        slot_labels = [str(s.get("label")) for s in slots if isinstance(s, dict) and s.get("label")]
        slot_part = ""
        if slot_labels[:2]:
            slot_part = f"Slots: {slot_labels[0]}{(' or ' + slot_labels[1]) if len(slot_labels) > 1 else ''}. "

        # One CTA: choose one slot (Reply 1/2) OR simple yes/no if no slots.
        if slot_labels:
            cta = "reply_1_or_2"
            cta_txt = "Reply 1 for the first slot, 2 for the second."
        else:
            cta = "yes_no"
            cta_txt = "Should we book it?"

        greeting = "Hi"
        if "hi-en" in lang.lower() or "mix" in lang.lower():
            greeting = "Hi"
        body = (
            f"{greeting} {cust_name}, {merchant_name} here. "
            f"{slot_part}{(offer_txt + '. ') if offer_txt else ''}"
            f"{cta_txt}"
        ).strip()

        return _base_compose(
            tid=tid,
            trigger=trigger,
            merchant=merchant,
            body=body,
            template_name="merchant_customer_outreach_v1",
            template_params=[cust_name, merchant_name],
            cta=cta,
            rationale="Customer-scope trigger; respects opt-in, uses real slots/offer when present, and keeps one clear CTA.",
        )

    # Fallback for unhandled kinds: still grounded, still one CTA.
    if scope == "merchant":
        body = (
            f"{prefix}, signal received for {merchant_name} ({kind}). "
            f"{fact and ('Current snapshot: ' + fact + '. ')}"
            "Want one specific recommendation now?"
        )
        return _base_compose(
            tid=tid,
            trigger=trigger,
            merchant=merchant,
            body=body,
            template_name="vera_generic_merchant_v1",
            template_params=[owner_first, kind, city],
            cta="yes_no",
            rationale="Fallback merchant flow stays deterministic, grounded, and asks one low-friction CTA.",
        )

    if scope == "customer":
        customer = customers.get(customer_id or "")
        if not customer:
            return None
        consent = _safe_get(customer, ["preferences", "reminder_opt_in"], True)
        if consent is False:
            return None
        cust_name = str(_safe_get(customer, ["identity", "name"], "there"))
        body = (
            f"Hi {cust_name}, {merchant_name} here. "
            f"{offer_txt and (offer_txt + '. ')}"
            "Want me to share one quick option for you right now?"
        )
        return _base_compose(
            tid=tid,
            trigger=trigger,
            merchant=merchant,
            body=body,
            template_name="merchant_customer_outreach_v1",
            template_params=[cust_name, merchant_name],
            cta="yes_no",
            rationale="Customer-scope fallback keeps outreach specific, consent-aware, and single-CTA.",
        )

    return None

