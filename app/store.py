from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Literal, Optional, Tuple

Scope = Literal["category", "merchant", "customer", "trigger"]


@dataclass(frozen=True)
class StoredContext:
    scope: Scope
    context_id: str
    version: int
    payload: Dict[str, Any]
    delivered_at: datetime
    stored_at: datetime


@dataclass
class ContextPutResult:
    accepted: bool
    stored: bool
    current_version: int
    ack_id: str
    stored_at: datetime
    reason: Optional[str] = None


class InMemoryStore:
    """
    Simple deterministic state store for the judge harness.
    - Context is versioned per (scope, context_id).
    - Higher version replaces atomically.
    - Same or lower version is rejected as stale (matches examples).
    """

    def __init__(self) -> None:
        self._contexts: Dict[Tuple[Scope, str], StoredContext] = {}
        self._conversations: Dict[str, Dict[str, Any]] = {}
        self._suppression: Dict[str, datetime] = {}

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def put_context(
        self,
        *,
        scope: Scope,
        context_id: str,
        version: int,
        payload: Dict[str, Any],
        delivered_at: datetime,
    ) -> ContextPutResult:
        key = (scope, context_id)
        existing = self._contexts.get(key)
        now = self._now()

        if existing is not None and version <= existing.version:
            return ContextPutResult(
                accepted=False,
                stored=False,
                current_version=existing.version,
                ack_id=f"ack_{scope}_{context_id}_v{existing.version}",
                stored_at=now,
                reason="stale_version",
            )

        stored = StoredContext(
            scope=scope,
            context_id=context_id,
            version=version,
            payload=payload,
            delivered_at=delivered_at,
            stored_at=now,
        )
        self._contexts[key] = stored
        return ContextPutResult(
            accepted=True,
            stored=True,
            current_version=version,
            ack_id=f"ack_{scope}_{context_id}_v{version}",
            stored_at=now,
        )

    def get_context(self, scope: Scope, context_id: str) -> Optional[StoredContext]:
        return self._contexts.get((scope, context_id))

    def contexts_by_scope(self, scope: Scope) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for (s, cid), ctx in self._contexts.items():
            if s == scope:
                out[cid] = ctx.payload
        return out

    def count_contexts(self) -> Dict[Scope, int]:
        counts: Dict[Scope, int] = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
        for (scope, _), _ctx in self._contexts.items():
            counts[scope] += 1
        return counts

    # Conversation helpers
    def upsert_conversation(self, conversation_id: str, data: Dict[str, Any]) -> None:
        self._conversations[conversation_id] = data

    def get_conversation(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        return self._conversations.get(conversation_id)

    # Suppression helpers
    def is_suppressed(self, suppression_key: str, now: datetime) -> bool:
        ts = self._suppression.get(suppression_key)
        return ts is not None and ts <= now

    def mark_suppressed(self, suppression_key: str, at: datetime) -> None:
        # Store the time we first used it; deterministic, no TTL needed for harness.
        self._suppression.setdefault(suppression_key, at)

    def suppression_keys(self) -> set[str]:
        return set(self._suppression.keys())

