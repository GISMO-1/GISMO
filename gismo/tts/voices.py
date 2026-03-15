"""Voice registry and model cache management."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

# ── Registry ───────────────────────────────────────────────────────────────

DEFAULT_VOICE = "en_GB-northern_english_male-medium"

VOICES: dict[str, dict] = {
    "en_GB-northern_english_male-medium": {
        "name": "Northern English Male",
        "lang": "en-GB",
        "quality": "medium",
        "description": "British male, northern accent",
    },
    "en_GB-alan-medium": {
        "name": "Alan",
        "lang": "en-GB",
        "quality": "medium",
        "description": "British male voice",
    },
    "en_US-lessac-medium": {
        "name": "Lessac",
        "lang": "en-US",
        "quality": "medium",
        "description": "American female voice",
    },
    "en_US-ryan-high": {
        "name": "Ryan",
        "lang": "en-US",
        "quality": "high",
        "description": "American male, high quality",
    },
    "en_US-amy-medium": {
        "name": "Amy",
        "lang": "en-US",
        "quality": "medium",
        "description": "American female voice",
    },
}


# ── Cache paths ────────────────────────────────────────────────────────────


def voices_dir() -> Path:
    """Return the directory where voice models are cached."""
    d = Path.home() / ".cache" / "gismo" / "tts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def model_path(voice_id: str) -> Path:
    return voices_dir() / f"{voice_id}.onnx"


def config_path(voice_id: str) -> Path:
    return voices_dir() / f"{voice_id}.onnx.json"


def is_downloaded(voice_id: str) -> bool:
    return model_path(voice_id).exists() and config_path(voice_id).exists()


def validate_voice(voice_id: str) -> None:
    if voice_id not in VOICES:
        known = ", ".join(sorted(VOICES))
        raise ValueError(f"Unknown voice '{voice_id}'. Known voices: {known}")


# ── Download ───────────────────────────────────────────────────────────────


def ensure_downloaded(
    voice_id: str,
    progress_cb: Callable[[str], None] | None = None,
) -> None:
    """Download model + config if not already cached."""
    validate_voice(voice_id)
    if is_downloaded(voice_id):
        return
    from piper.download_voices import download_voice

    if progress_cb:
        progress_cb(f"Downloading voice model '{voice_id}'…")
    download_voice(voice_id, voices_dir())
    if progress_cb:
        progress_cb(f"Downloaded '{voice_id}'.")
