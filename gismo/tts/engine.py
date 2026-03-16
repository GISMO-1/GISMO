"""TTS synthesis engine — kokoro-onnx primary, piper fallback."""
from __future__ import annotations

import io
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path
from typing import Callable

from gismo.tts.voices import (
    ENGINE_KOKORO,
    ensure_downloaded,
    kokoro_model_path,
    kokoro_voices_path,
    model_path,
    voice_engine,
)


def _preprocess(text: str) -> str:
    """Normalise text before synthesis."""
    import re
    # Preserve pronunciation: replace GISMO (case-insensitive) with phonetic spelling
    return re.sub(r'\bGISMO\b', 'GHIZMO', text, flags=re.IGNORECASE)


# ── Kokoro synthesis ────────────────────────────────────────────────────────


def _synthesize_kokoro(text: str, voice_id: str) -> bytes:
    """Synthesize using kokoro-onnx; returns WAV bytes."""
    import struct

    import numpy as np
    from kokoro_onnx import Kokoro

    # lang derived from voice prefix: af/am → en-us, bf/bm → en-gb
    prefix = voice_id[:2]
    lang = "en-gb" if prefix in ("bf", "bm") else "en-us"

    kokoro = Kokoro(str(kokoro_model_path()), str(kokoro_voices_path()))
    samples, sample_rate = kokoro.create(_preprocess(text), voice=voice_id, speed=1.0, lang=lang)

    # Convert float32 samples to 16-bit PCM WAV
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm.tobytes())
    return buf.getvalue()


# ── Piper synthesis ─────────────────────────────────────────────────────────


def _synthesize_piper(text: str, voice_id: str) -> bytes:
    """Synthesize using piper-tts; returns WAV bytes."""
    from piper.voice import PiperVoice

    voice = PiperVoice.load(str(model_path(voice_id)))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav_file:
        voice.synthesize_wav(_preprocess(text), wav_file)
    return buf.getvalue()


# ── Public API ──────────────────────────────────────────────────────────────


def synthesize(
    text: str,
    voice_id: str,
    progress_cb: Callable[[str], None] | None = None,
) -> bytes:
    """Synthesize *text* with *voice_id* and return WAV bytes.

    Uses kokoro-onnx for kokoro voices, piper-tts for piper voices.
    Downloads model files on first use.
    """
    ensure_downloaded(voice_id, progress_cb=progress_cb)

    if voice_engine(voice_id) == ENGINE_KOKORO:
        return _synthesize_kokoro(text, voice_id)
    return _synthesize_piper(text, voice_id)


def play(wav_bytes: bytes) -> None:
    """Play WAV bytes using the system audio player (blocking)."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(wav_bytes)
        tmp_path = f.name
    try:
        if sys.platform == "win32":
            import winsound
            winsound.PlaySound(tmp_path, winsound.SND_FILENAME)
        elif sys.platform == "darwin":
            subprocess.run(["afplay", tmp_path], check=False)
        else:
            for player in ("aplay", "paplay", "play"):
                if shutil.which(player):
                    subprocess.run([player, tmp_path], check=False)
                    break
    finally:
        Path(tmp_path).unlink(missing_ok=True)
