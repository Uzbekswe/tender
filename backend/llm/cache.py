"""LLM response cache and call log, stored in the LlmCall table (ARCHITECTURE §6.4).

The caller owns the session; this module never creates engines or commits
anything except the single row written by ``put``.
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any

from sqlmodel import Session, select

from app.models import LlmCall


def cache_key(
    provider: str,
    model: str,
    prompt_version: str,
    messages: Sequence[Mapping[str, Any]],
    image_hashes: Sequence[str],
    temperature: float,
    schema_name: str,
    extra: Mapping[str, Any] | None = None,
) -> str:
    """SHA-256 of a canonical JSON form of the request. Dict key order does not matter.

    `extra` holds settings that change the output (e.g. effort). It is left out of the
    payload when empty, so keys of calls made without it do not change.
    """
    payload = {
        "provider": provider,
        "model": model,
        "prompt_version": prompt_version,
        "messages": [dict(message) for message in messages],
        "image_hashes": list(image_hashes),
        "temperature": float(temperature),  # 0 and 0.0 must give the same key
        "schema": schema_name,
    }
    if extra:
        payload["extra"] = dict(extra)
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def get(session: Session, key: str) -> LlmCall | None:
    return session.exec(select(LlmCall).where(LlmCall.key == key)).first()


def put(session: Session, call: LlmCall) -> LlmCall:
    """Store a call. If the key is already stored (e.g. two identical parallel calls), return that row."""
    existing = get(session, call.key)
    if existing is not None:
        return existing
    session.add(call)
    session.commit()
    return call
