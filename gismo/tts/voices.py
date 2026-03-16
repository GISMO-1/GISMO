"""Voice registry and model cache management for kokoro and piper engines."""
from __future__ import annotations

import urllib.request
from pathlib import Path
from typing import Callable

# ── Engine constants ────────────────────────────────────────────────────────

ENGINE_KOKORO = "kokoro"
ENGINE_PIPER = "piper"

# ── Registry ────────────────────────────────────────────────────────────────

DEFAULT_VOICE = "bm_lewis"

VOICES: dict[str, dict] = {
    # ── Kokoro voices ───────────────────────────────────────────────────────
    "af_heart": {
        "name": "Heart",
        "lang": "en-US",
        "quality": "high",
        "description": "American female, warm and expressive",
        "engine": ENGINE_KOKORO,
    },
    "af_bella": {
        "name": "Bella",
        "lang": "en-US",
        "quality": "high",
        "description": "American female, smooth and clear",
        "engine": ENGINE_KOKORO,
    },
    "af_nicole": {
        "name": "Nicole",
        "lang": "en-US",
        "quality": "high",
        "description": "American female, natural and bright",
        "engine": ENGINE_KOKORO,
    },
    "af_sarah": {
        "name": "Sarah",
        "lang": "en-US",
        "quality": "high",
        "description": "American female, conversational",
        "engine": ENGINE_KOKORO,
    },
    "af_sky": {
        "name": "Sky",
        "lang": "en-US",
        "quality": "high",
        "description": "American female, light and airy",
        "engine": ENGINE_KOKORO,
    },
    "am_adam": {
        "name": "Adam",
        "lang": "en-US",
        "quality": "high",
        "description": "American male, clear and confident",
        "engine": ENGINE_KOKORO,
    },
    "am_michael": {
        "name": "Michael",
        "lang": "en-US",
        "quality": "high",
        "description": "American male, deep and steady",
        "engine": ENGINE_KOKORO,
    },
    "bf_emma": {
        "name": "Emma",
        "lang": "en-GB",
        "quality": "high",
        "description": "British female, professional",
        "engine": ENGINE_KOKORO,
    },
    "bf_isabella": {
        "name": "Isabella",
        "lang": "en-GB",
        "quality": "high",
        "description": "British female, warm and measured",
        "engine": ENGINE_KOKORO,
    },
    "bm_george": {
        "name": "George",
        "lang": "en-GB",
        "quality": "high",
        "description": "British male, authoritative",
        "engine": ENGINE_KOKORO,
    },
    "bm_lewis": {
        "name": "Lewis",
        "lang": "en-GB",
        "quality": "high",
        "description": "British male, calm and clear (default)",
        "engine": ENGINE_KOKORO,
    },
    # ── Piper voices (fallback) ─────────────────────────────────────────────
    "en_GB-northern_english_male-medium": {
        "name": "Northern English Male",
        "lang": "en-GB",
        "quality": "medium",
        "description": "British male, northern accent (piper)",
        "engine": ENGINE_PIPER,
    },
    "en_GB-alan-medium": {
        "name": "Alan",
        "lang": "en-GB",
        "quality": "medium",
        "description": "British male voice (piper)",
        "engine": ENGINE_PIPER,
    },
    "en_US-lessac-medium": {
        "name": "Lessac",
        "lang": "en-US",
        "quality": "medium",
        "description": "American female voice (piper)",
        "engine": ENGINE_PIPER,
    },
    "en_US-ryan-high": {
        "name": "Ryan",
        "lang": "en-US",
        "quality": "high",
        "description": "American male, high quality (piper)",
        "engine": ENGINE_PIPER,
    },
    "en_US-amy-medium": {
        "name": "Amy",
        "lang": "en-US",
        "quality": "medium",
        "description": "American female voice (piper)",
        "engine": ENGINE_PIPER,
    },
}


# ── Cache paths ─────────────────────────────────────────────────────────────


def voices_dir() -> Path:
    """Return the directory where piper voice models are cached."""
    d = Path.home() / ".cache" / "gismo" / "tts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def kokoro_dir() -> Path:
    """Return the directory where kokoro model files are cached."""
    d = voices_dir() / "kokoro"
    d.mkdir(parents=True, exist_ok=True)
    return d


def kokoro_model_path() -> Path:
    return kokoro_dir() / "kokoro-v1.0.onnx"


def kokoro_voices_path() -> Path:
    return kokoro_dir() / "voices-v1.0.bin"


def model_path(voice_id: str) -> Path:
    """Return the piper .onnx path for a piper voice."""
    return voices_dir() / f"{voice_id}.onnx"


def config_path(voice_id: str) -> Path:
    return voices_dir() / f"{voice_id}.onnx.json"


def voice_engine(voice_id: str) -> str:
    """Return the engine string for a registered voice."""
    return VOICES[voice_id]["engine"]


def is_kokoro_downloaded() -> bool:
    return kokoro_model_path().exists() and kokoro_voices_path().exists()


def is_downloaded(voice_id: str) -> bool:
    info = VOICES.get(voice_id, {})
    if info.get("engine") == ENGINE_KOKORO:
        return is_kokoro_downloaded()
    return model_path(voice_id).exists() and config_path(voice_id).exists()


def validate_voice(voice_id: str) -> None:
    if voice_id not in VOICES:
        known = ", ".join(sorted(VOICES))
        raise ValueError(f"Unknown voice '{voice_id}'. Known voices: {known}")


# ── Download ────────────────────────────────────────────────────────────────

_KOKORO_MODEL_URL = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
    "model-files-v1.0/kokoro-v1.0.onnx"
)
_KOKORO_VOICES_URL = (
    "https://github.com/thewh1teagle/kokoro-onnx/releases/download/"
    "model-files-v1.0/voices-v1.0.bin"
)


def _download_file(url: str, dest: Path, progress_cb: Callable[[str], None] | None) -> None:
    if progress_cb:
        progress_cb(f"Downloading {dest.name}…")
    urllib.request.urlretrieve(url, dest)
    if progress_cb:
        size_mb = dest.stat().st_size / 1e6
        progress_cb(f"Downloaded {dest.name} ({size_mb:.1f} MB)")


def ensure_kokoro_downloaded(progress_cb: Callable[[str], None] | None = None) -> None:
    """Download kokoro model files if not already cached."""
    if not kokoro_model_path().exists():
        _download_file(_KOKORO_MODEL_URL, kokoro_model_path(), progress_cb)
    if not kokoro_voices_path().exists():
        _download_file(_KOKORO_VOICES_URL, kokoro_voices_path(), progress_cb)


def ensure_downloaded(
    voice_id: str,
    progress_cb: Callable[[str], None] | None = None,
) -> None:
    """Download model files for *voice_id* if not already cached."""
    validate_voice(voice_id)
    if is_downloaded(voice_id):
        return
    if VOICES[voice_id]["engine"] == ENGINE_KOKORO:
        ensure_kokoro_downloaded(progress_cb)
    else:
        from piper.download_voices import download_voice

        if progress_cb:
            progress_cb(f"Downloading voice model '{voice_id}'…")
        download_voice(voice_id, voices_dir())
        if progress_cb:
            progress_cb(f"Downloaded '{voice_id}'.")
