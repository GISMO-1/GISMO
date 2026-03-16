"""First-run onboarding for GISMO — shared by CLI and web entry points."""
from __future__ import annotations

_NAMESPACE = "gismo:settings"
_KEY_OPERATOR_NAME = "operator.name"
_ACTOR = "onboarding"
_POLICY_HASH = "internal"


# ── Memory helpers ──────────────────────────────────────────────────────────


def get_operator_name(db_path: str) -> str | None:
    """Return the stored operator name, or None if not yet set."""
    try:
        from gismo.memory.store import MemoryStore

        with MemoryStore(db_path) as store:
            with store._connection() as conn:
                item = store._fetch_item(
                    conn,
                    namespace=_NAMESPACE,
                    key=_KEY_OPERATOR_NAME,
                    include_tombstoned=False,
                )
        if item is not None:
            return str(item.value)
    except Exception:
        pass
    return None


def set_operator_name(db_path: str, name: str) -> None:
    """Persist operator name to memory."""
    from gismo.memory.store import put_item

    put_item(
        db_path,
        namespace=_NAMESPACE,
        key=_KEY_OPERATOR_NAME,
        kind="preference",
        value=name,
        tags=["operator", "identity"],
        confidence="high",
        source="operator",
        ttl_seconds=None,
        actor=_ACTOR,
        policy_hash=_POLICY_HASH,
    )


def needs_onboarding(db_path: str) -> bool:
    """Return True if operator name has not been set."""
    try:
        return get_operator_name(db_path) is None
    except Exception:
        return False


# ── CLI onboarding ──────────────────────────────────────────────────────────

_BANNER = """
╔══════════════════════════════════════════════╗
║                                              ║
║         W E L C O M E   T O                 ║
║                                              ║
║          G · I · S · M · O                  ║
║                                              ║
║   Local-first personal AI assistant         ║
║   Built by Mike Burns                       ║
║                                              ║
╚══════════════════════════════════════════════╝
"""


def run_cli_onboarding(db_path: str) -> None:
    """Run the interactive CLI first-run setup flow."""
    from gismo.tts.voices import DEFAULT_VOICE, ENGINE_KOKORO, VOICES
    from gismo.tts.prefs import set_voice

    print(_BANNER)
    print("First-time setup — this only runs once.\n")

    # ── Step 1: operator name ───────────────────────────────────────────────
    try:
        name = input("What should I call you? ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nSetup cancelled.")
        return
    if not name:
        name = "Operator"

    # ── Step 2: voice selection (kokoro voices only) ────────────────────────
    kokoro_voices = [
        (vid, info) for vid, info in VOICES.items() if info["engine"] == ENGINE_KOKORO
    ]
    default_idx = next(
        (i for i, (vid, _) in enumerate(kokoro_voices) if vid == DEFAULT_VOICE), 0
    )

    print("\nAvailable voices:\n")
    for i, (vid, info) in enumerate(kokoro_voices):
        marker = "  ← default" if i == default_idx else ""
        print(f"  {i+1:2}.  {info['name']:<12}  {info['lang']:<8}  {info['description']}{marker}")

    print()
    try:
        choice_raw = input(
            f"Pick a voice [1-{len(kokoro_voices)}] (press Enter for default): "
        ).strip()
    except (EOFError, KeyboardInterrupt):
        print("\nSetup cancelled.")
        return

    if choice_raw == "":
        chosen_idx = default_idx
    else:
        try:
            chosen_idx = int(choice_raw) - 1
            if not (0 <= chosen_idx < len(kokoro_voices)):
                chosen_idx = default_idx
        except ValueError:
            chosen_idx = default_idx

    chosen_voice_id, chosen_voice_info = kokoro_voices[chosen_idx]

    # ── Save preferences ────────────────────────────────────────────────────
    set_operator_name(db_path, name)
    set_voice(db_path, chosen_voice_id)

    print(f"\nHello, {name}. Voice set to {chosen_voice_info['name']}.\n")

    # ── Speak welcome ───────────────────────────────────────────────────────
    welcome = f"Welcome, {name}. I'm yours."
    try:
        from gismo.tts.engine import play, synthesize

        print(f'Speaking: "{welcome}"', flush=True)
        wav = synthesize(
            welcome,
            chosen_voice_id,
            progress_cb=lambda m: print(f"  {m}", flush=True),
        )
        play(wav)
    except Exception as exc:
        print(f"  (TTS unavailable: {exc})")

    print("\nSetup complete. GISMO is ready.\n")
