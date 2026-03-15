"""Voice preference stored in GISMO memory (namespace gismo:settings)."""
from __future__ import annotations

from gismo.tts.voices import DEFAULT_VOICE, validate_voice

_NAMESPACE = "gismo:settings"
_KEY = "tts.voice"
_ACTOR = "tts-system"
_POLICY_HASH = "internal"


def get_voice(db_path: str) -> str:
    """Return the stored voice preference, or DEFAULT_VOICE if none set."""
    try:
        from gismo.memory.store import MemoryStore

        with MemoryStore(db_path) as store:
            with store._connection() as connection:
                item = store._fetch_item(
                    connection,
                    namespace=_NAMESPACE,
                    key=_KEY,
                    include_tombstoned=False,
                )
        if item is not None:
            return str(item.value)
    except Exception:
        pass
    return DEFAULT_VOICE


def set_voice(db_path: str, voice_id: str) -> None:
    """Persist voice preference to memory."""
    validate_voice(voice_id)
    from gismo.memory.store import put_item

    put_item(
        db_path,
        namespace=_NAMESPACE,
        key=_KEY,
        kind="preference",
        value=voice_id,
        tags=["tts", "voice"],
        confidence="high",
        source="operator",
        ttl_seconds=None,
        actor=_ACTOR,
        policy_hash=_POLICY_HASH,
    )
