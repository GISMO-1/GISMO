"""Tests for gismo.tts — voices, prefs, web API."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from gismo.core.state import StateStore
from gismo.tts.voices import (
    VOICES,
    DEFAULT_VOICE,
    validate_voice,
    is_downloaded,
    model_path,
    config_path,
    voices_dir,
)


def _make_db(tmp: str) -> str:
    db_path = str(Path(tmp) / "state.db")
    with StateStore(db_path) as store:
        store.create_run(label="test")
    return db_path


# ── Voice registry ─────────────────────────────────────────────────────────

class TestVoiceRegistry(unittest.TestCase):
    def test_default_voice_is_in_registry(self) -> None:
        self.assertIn(DEFAULT_VOICE, VOICES)

    def test_all_five_voices_present(self) -> None:
        expected = {
            "en_GB-northern_english_male-medium",
            "en_GB-alan-medium",
            "en_US-lessac-medium",
            "en_US-ryan-high",
            "en_US-amy-medium",
        }
        self.assertEqual(set(VOICES.keys()), expected)

    def test_each_voice_has_required_fields(self) -> None:
        for vid, info in VOICES.items():
            for field in ("name", "lang", "quality", "description"):
                self.assertIn(field, info, f"Voice {vid!r} missing field {field!r}")

    def test_validate_voice_accepts_known(self) -> None:
        # Should not raise
        validate_voice(DEFAULT_VOICE)

    def test_validate_voice_rejects_unknown(self) -> None:
        with self.assertRaises(ValueError):
            validate_voice("en_XX-fake-high")

    def test_voices_dir_returns_path(self) -> None:
        d = voices_dir()
        self.assertIsInstance(d, Path)
        self.assertTrue(d.exists())

    def test_model_path_format(self) -> None:
        mp = model_path(DEFAULT_VOICE)
        self.assertTrue(str(mp).endswith(f"{DEFAULT_VOICE}.onnx"))

    def test_config_path_format(self) -> None:
        cp = config_path(DEFAULT_VOICE)
        self.assertTrue(str(cp).endswith(f"{DEFAULT_VOICE}.onnx.json"))

    def test_is_downloaded_false_when_missing(self) -> None:
        # Model files are not present in test environment
        self.assertIsInstance(is_downloaded("en_US-amy-medium"), bool)

    def test_ensure_downloaded_calls_download_voice(self) -> None:
        from gismo.tts.voices import ensure_downloaded
        with patch("gismo.tts.voices.is_downloaded", return_value=True):
            # Already downloaded — should not call download_voice
            ensure_downloaded(DEFAULT_VOICE)  # no error


# ── Prefs ──────────────────────────────────────────────────────────────────

class TestTtsPrefs(unittest.TestCase):
    def test_get_voice_returns_default_when_no_pref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            from gismo.tts.prefs import get_voice
            self.assertEqual(get_voice(db), DEFAULT_VOICE)

    def test_set_and_get_voice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            from gismo.tts.prefs import set_voice, get_voice
            set_voice(db, "en_US-ryan-high")
            self.assertEqual(get_voice(db), "en_US-ryan-high")

    def test_set_voice_rejects_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            from gismo.tts.prefs import set_voice
            with self.assertRaises(ValueError):
                set_voice(db, "en_XX-fake-high")

    def test_set_voice_updates_preference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            from gismo.tts.prefs import set_voice, get_voice
            set_voice(db, "en_US-lessac-medium")
            set_voice(db, "en_GB-alan-medium")
            self.assertEqual(get_voice(db), "en_GB-alan-medium")


# ── Web API ────────────────────────────────────────────────────────────────

class TestWebApiVoices(unittest.TestCase):
    def test_get_voices_returns_all_voices(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            from gismo.web.api import get_voices
            data = get_voices(db)
            self.assertIn("voices", data)
            self.assertIn("current", data)
            self.assertEqual(len(data["voices"]), len(VOICES))

    def test_get_voices_marks_current(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            from gismo.web.api import get_voices, set_voice_preference
            set_voice_preference(db, "en_US-ryan-high")
            data = get_voices(db)
            self.assertEqual(data["current"], "en_US-ryan-high")
            selected = [v for v in data["voices"] if v["is_selected"]]
            self.assertEqual(len(selected), 1)
            self.assertEqual(selected[0]["id"], "en_US-ryan-high")

    def test_get_voices_marks_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            from gismo.web.api import get_voices
            data = get_voices(db)
            defaults = [v for v in data["voices"] if v["is_default"]]
            self.assertEqual(len(defaults), 1)
            self.assertEqual(defaults[0]["id"], DEFAULT_VOICE)

    def test_get_voices_has_downloaded_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            from gismo.web.api import get_voices
            data = get_voices(db)
            for v in data["voices"]:
                self.assertIn("downloaded", v)
                self.assertIsInstance(v["downloaded"], bool)

    def test_set_voice_preference_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            from gismo.web.api import set_voice_preference, get_voices
            result = set_voice_preference(db, "en_US-amy-medium")
            self.assertEqual(result["voice"], "en_US-amy-medium")
            data = get_voices(db)
            self.assertEqual(data["current"], "en_US-amy-medium")

    def test_set_voice_preference_rejects_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            from gismo.web.api import set_voice_preference
            with self.assertRaises(ValueError):
                set_voice_preference(db, "en_XX-fake-high")


# ── Text preprocessing ─────────────────────────────────────────────────────

class TestPreprocess(unittest.TestCase):
    def _pp(self, text: str) -> str:
        from gismo.tts.engine import _preprocess
        return _preprocess(text)

    def test_gismo_uppercase_replaced(self) -> None:
        self.assertEqual(self._pp("Hello GISMO"), "Hello GHIZMO")

    def test_gismo_lowercase_replaced(self) -> None:
        self.assertEqual(self._pp("hello gismo"), "hello GHIZMO")

    def test_gismo_mixed_case_replaced(self) -> None:
        self.assertEqual(self._pp("Gismo is running"), "GHIZMO is running")

    def test_gismo_multiple_occurrences(self) -> None:
        self.assertEqual(self._pp("GISMO GISMO"), "GHIZMO GHIZMO")

    def test_no_false_positive_substring(self) -> None:
        # "GISMO" inside another word should not be replaced
        self.assertEqual(self._pp("MEGISMO"), "MEGISMO")

    def test_unrelated_text_unchanged(self) -> None:
        self.assertEqual(self._pp("Hello world"), "Hello world")


# ── Synthesis (mocked) ─────────────────────────────────────────────────────

class TestSynthesisMocked(unittest.TestCase):
    def test_synthesize_calls_piper_voice(self) -> None:
        """Verify engine.synthesize downloads and calls PiperVoice.load."""
        import io, wave
        from gismo.tts import engine

        # Build a minimal valid WAV buffer as mock output
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(22050)
            wf.writeframes(b"\x00\x00" * 100)
        fake_wav = buf.getvalue()

        mock_voice = MagicMock()
        def _fake_synthesize_wav(text, wav_file, **kw):
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(22050)
            wav_file.writeframes(b"\x00\x00" * 100)

        mock_voice.synthesize_wav.side_effect = _fake_synthesize_wav

        with patch("gismo.tts.engine.ensure_downloaded"), \
             patch("piper.voice.PiperVoice") as mock_piper:
            mock_piper.load.return_value = mock_voice
            result = engine.synthesize("Hello world", DEFAULT_VOICE)

        self.assertIsInstance(result, bytes)
        self.assertTrue(result.startswith(b"RIFF"))  # WAV magic bytes

    def test_web_api_tts_synthesize_uses_preference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = _make_db(tmp)
            import io, wave
            from gismo.web import api as web_api

            fake_wav = io.BytesIO()
            with wave.open(fake_wav, "wb") as wf:
                wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(22050)
                wf.writeframes(b"\x00\x00" * 10)

            with patch("gismo.tts.engine.synthesize", return_value=fake_wav.getvalue()) as mock_synth:
                result = web_api.tts_synthesize(db, "Hello", voice_id=None)

            # Should have called synthesize with the default voice
            mock_synth.assert_called_once()
            call_voice = mock_synth.call_args[0][1]
            self.assertEqual(call_voice, DEFAULT_VOICE)
            self.assertTrue(result.startswith(b"RIFF"))


if __name__ == "__main__":
    unittest.main()
