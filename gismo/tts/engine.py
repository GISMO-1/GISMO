"""piper-tts synthesis engine."""
from __future__ import annotations

import io
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path
from typing import Callable

from gismo.tts.voices import ensure_downloaded, model_path


def synthesize(
    text: str,
    voice_id: str,
    progress_cb: Callable[[str], None] | None = None,
) -> bytes:
    """Synthesize *text* with *voice_id* and return WAV bytes.

    Downloads the model on first use.
    """
    ensure_downloaded(voice_id, progress_cb=progress_cb)

    from piper.voice import PiperVoice

    mp = str(model_path(voice_id))
    voice = PiperVoice.load(mp)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav_file:
        voice.synthesize_wav(text, wav_file)
    return buf.getvalue()


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
