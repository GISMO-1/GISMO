"""CLI handlers for `gismo tts` subcommands."""
from __future__ import annotations

import argparse
import sys


def handle_voices_list(args: argparse.Namespace) -> None:
    from gismo.tts.voices import VOICES, DEFAULT_VOICE, is_downloaded
    from gismo.tts.prefs import get_voice

    current = get_voice(args.db_path)
    rows = []
    for vid, info in VOICES.items():
        flags = []
        if vid == current:
            flags.append("selected")
        if vid == DEFAULT_VOICE:
            flags.append("default")
        if is_downloaded(vid):
            flags.append("downloaded")
        badge = f"  [{', '.join(flags)}]" if flags else ""
        rows.append(f"  {vid:<40} {info['lang']:<8} {info['quality']:<8}{badge}")

    print("Available voices:")
    for row in rows:
        print(row)
    print()
    print(f"Current preference: {current}")


def handle_voices_set(args: argparse.Namespace) -> None:
    from gismo.tts.prefs import set_voice
    from gismo.tts.voices import validate_voice

    try:
        validate_voice(args.voice)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc

    set_voice(args.db_path, args.voice)
    print(f"Voice preference set to: {args.voice}")


def handle_voices_download(args: argparse.Namespace) -> None:
    from gismo.tts.voices import ensure_downloaded, validate_voice

    try:
        validate_voice(args.voice)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc

    ensure_downloaded(args.voice, progress_cb=print)
    print(f"Ready: {args.voice}")


def handle_speak(args: argparse.Namespace) -> None:
    from gismo.tts.engine import synthesize, play
    from gismo.tts.prefs import get_voice
    from gismo.tts.voices import validate_voice

    voice = args.voice or get_voice(args.db_path)

    try:
        validate_voice(voice)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc

    print(f"Synthesizing with voice '{voice}'…", file=sys.stderr)
    wav_bytes = synthesize(args.text, voice, progress_cb=lambda m: print(m, file=sys.stderr))

    if args.out:
        with open(args.out, "wb") as f:
            f.write(wav_bytes)
        print(f"Written to: {args.out}")
    elif not args.no_play:
        play(wav_bytes)
    else:
        sys.stdout.buffer.write(wav_bytes)
