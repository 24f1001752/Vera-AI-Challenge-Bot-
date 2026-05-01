#!/usr/bin/env python3
"""
Contract smoke test that does NOT require an LLM API key.

It validates:
- /v1/healthz and /v1/metadata are live
- /v1/context accepts category/merchant/customer/trigger and enforces versioning
- /v1/tick returns <= 20 actions with required fields
- /v1/reply returns a valid action shape
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib import request as urlrequest
from urllib.error import HTTPError


BOT_URL = os.environ.get("BOT_URL", "http://127.0.0.1:8080").rstrip("/")
DATASET_DIR = Path(__file__).parent / "dataset"
EXPANDED_DIR = Path(__file__).parent / "expanded"


def _http(method: str, path: str, payload: Dict[str, Any] | None = None) -> Tuple[int, Dict[str, Any]]:
    url = f"{BOT_URL}{path}"
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urlrequest.Request(url, method=method, data=data, headers=headers)
    try:
        with urlrequest.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body) if body else {}
    except HTTPError as e:
        body = e.read().decode("utf-8")
        try:
            return e.code, json.loads(body) if body else {}
        except json.JSONDecodeError:
            return e.code, {"raw": body}


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def ensure_expanded() -> None:
    if EXPANDED_DIR.exists():
        return
    EXPANDED_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.check_call(
        [sys.executable, str(DATASET_DIR / "generate_dataset.py"), "--seed-dir", str(DATASET_DIR), "--out", str(EXPANDED_DIR)]
    )


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def assert_context_upsert(scope: str, context_id: str, payload: Dict[str, Any], version: int = 1) -> None:
    code, resp = _http(
        "POST",
        "/v1/context",
        {
            "scope": scope,
            "context_id": context_id,
            "version": version,
            "payload": payload,
            "delivered_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    # Fresh server: 200 accepted.
    if code == 200 and resp.get("accepted") is True:
        return
    # Re-run against warm server: 409 stale_version is expected.
    if code == 409 and resp.get("reason") == "stale_version":
        return
    raise AssertionError(f"{scope} push failed: {code} {resp}")


def main() -> None:
    print(f"[INFO] Bot URL: {BOT_URL}")
    code, hz = _http("GET", "/v1/healthz")
    _assert(code == 200, f"healthz expected 200, got {code} {hz}")
    _assert(hz.get("status") == "ok", f"healthz status not ok: {hz}")

    code, meta = _http("GET", "/v1/metadata")
    _assert(code == 200, f"metadata expected 200, got {code} {meta}")
    for k in ["team_name", "team_members", "model", "approach", "contact_email", "version", "submitted_at"]:
        _assert(k in meta, f"metadata missing {k}")

    ensure_expanded()

    # Push 5 categories
    for f in (EXPANDED_DIR / "categories").glob("*.json"):
        payload = load_json(f)
        assert_context_upsert("category", payload["slug"], payload, version=1)

    # Push a few merchants/customers
    merchant_files = sorted((EXPANDED_DIR / "merchants").glob("*.json"))[:5]
    customer_files = sorted((EXPANDED_DIR / "customers").glob("*.json"))[:8]
    trigger_files = sorted((EXPANDED_DIR / "triggers").glob("*.json"))[:12]

    for f in merchant_files:
        payload = load_json(f)
        assert_context_upsert("merchant", payload["merchant_id"], payload, version=1)

        # Idempotency: re-push same version should 409
        code2, resp2 = _http(
            "POST",
            "/v1/context",
            {
                "scope": "merchant",
                "context_id": payload["merchant_id"],
                "version": 1,
                "payload": payload,
                "delivered_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        _assert(code2 == 409 and resp2.get("reason") == "stale_version", f"expected 409 stale_version, got {code2} {resp2}")

    for f in customer_files:
        payload = load_json(f)
        assert_context_upsert("customer", payload["customer_id"], payload, version=1)

    # Push triggers and tick
    trigger_ids: List[str] = []
    for f in trigger_files:
        payload = load_json(f)
        trigger_ids.append(payload["id"])
        assert_context_upsert("trigger", payload["id"], payload, version=1)

    code, tick_resp = _http(
        "POST",
        "/v1/tick",
        {"now": datetime.now(timezone.utc).isoformat(), "available_triggers": trigger_ids},
    )
    _assert(code == 200, f"tick expected 200, got {code} {tick_resp}")
    actions = tick_resp.get("actions", [])
    _assert(isinstance(actions, list), f"tick.actions not a list: {tick_resp}")
    _assert(len(actions) <= 20, f"tick returned >20 actions: {len(actions)}")

    required = {
        "conversation_id",
        "merchant_id",
        "customer_id",
        "send_as",
        "trigger_id",
        "template_name",
        "template_params",
        "body",
        "cta",
        "suppression_key",
        "rationale",
    }
    for a in actions:
        _assert(isinstance(a, dict), f"action not dict: {a}")
        missing = sorted(required - set(a.keys()))
        _assert(not missing, f"action missing fields {missing}: {a}")

    # Reply to first action (if any) and validate reply schema
    if actions:
        a0 = actions[0]
        code, rep = _http(
            "POST",
            "/v1/reply",
            {
                "conversation_id": a0["conversation_id"],
                "merchant_id": a0["merchant_id"],
                "customer_id": a0.get("customer_id"),
                "from_role": "merchant",
                "message": "yes",
                "received_at": datetime.now(timezone.utc).isoformat(),
                "turn_number": 2,
            },
        )
        _assert(code == 200, f"reply expected 200, got {code} {rep}")
        _assert(rep.get("action") in ["send", "wait", "close"], f"invalid reply action: {rep}")


if __name__ == "__main__":
    try:
        main()
        print("[PASS] Contract smoke test complete.")
    except Exception as e:
        print(f"[FAIL] {e}")
        sys.exit(1)

