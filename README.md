# GISMO

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey.svg)]()
[![GitHub Stars](https://img.shields.io/github/stars/GISMO-1/GISMO?style=social)](https://github.com/GISMO-1/GISMO/stargazers)

> Your personal AI. Local. Private. Yours.

GISMO (General Intelligent System for Multiflow Operations) is a local-first personal AI assistant that runs entirely on your hardware. No cloud. No subscriptions. No data leaving your machine.

Talk to GISMO naturally. Control your smart home. Monitor your cameras. Manage your tasks. Everything private, everything logged, everything under your control.

## What GISMO Can Do

- **Chat naturally** — Talk to GISMO by text or voice. It understands context and remembers your preferences.
- **Control smart devices** — Connect cameras, smart lights, thermostats, and sensors. GISMO discovers devices on your network automatically.
- **Monitor your home** — Live camera feeds, system health, activity alerts — all in one command center dashboard.
- **Manage tasks** — Queue up operations, review plans before they execute, track everything.
- **Stay private** — Everything runs locally. Your conversations, your device data, your preferences — nothing leaves your machine. Ever.
- **Speak to you** — GISMO has a voice (Kokoro TTS with 11 voices to choose from).
- **Remember things** — Persistent memory stores your preferences, notes, and context across sessions.
- **First-run onboarding** — GISMO introduces itself, learns your name, lets you pick a voice, and personalizes your experience.

## Screenshot

<img width="1920" height="1032" alt="image" src="https://github.com/user-attachments/assets/f202ca68-507a-4f95-bdd8-1bd4ccb2f987" />

## Quick Start

### Prerequisites
- Python 3.11+
- Ollama (for the AI brain)
- Windows 10/11, Linux, or macOS

### Install
```bash
git clone https://github.com/GISMO-1/GISMO.git
cd GISMO
python -m venv .venv

# Windows
.venv\Scripts\Activate.ps1

# Linux/macOS
source .venv/bin/activate

pip install -e .
```

### Launch
```bash
# Desktop app (recommended)
gismo app

# Web dashboard
gismo web

# CLI
gismo ask "What can you do?"
```

On first launch, GISMO walks you through setup — your name, preferred voice, and you're ready to go.

## The Command Center

GISMO opens as a desktop application with a mission control layout:

- **Left panel** — Connected devices with live status
- **Center** — Chat with GISMO (the main interaction)
- **Right panel** — Activity feed and task queue
- **Top bar** — System status, search, your name

## Architecture

- **Python** codebase, **SQLite** for state, **Ollama** for the AI brain
- **Kokoro TTS** for voice (with Piper as fallback)
- **pywebview** for the native desktop window
- Policy-gated execution — GISMO only does what you allow
- Full audit trail — every action is logged

## For Developers

See [docs/OPERATOR.md](docs/OPERATOR.md) for detailed CLI commands, policy configuration, memory management, agent loops, and the full technical reference.

## Status

- **Phase 0-2** — Foundation, planner, guardrails ✅
- **Phase 3** — Memory and context ✅
- **Phase 4** — Interactive experience (command center, voice, desktop app, onboarding) 🔄
- **Phase 5** — Device connections (cameras, lights, sensors) 🔄
- **Future** — Always-on service, installer, mobile access, Earthship integration

## Origin

GISMO was born on Christmas Day 2025. Built by a factory worker who runs industrial robots by day and codes by night. The name is a nod to Gizmo from Gremlins — friendly and helpful, but with rules to keep things safe.

## License

MIT License — free to use, modify, and distribute.

## Links

- [Operator Guide](docs/OPERATOR.md)
- [GitHub Sponsors](https://github.com/sponsors/GISMO-1)
- [Dev.to Article](https://dev.to/leo_burns_f4fa35f1cc6eeba/i-built-a-local-first-ai-orchestration-system-on-a-7-year-old-laptop-1nl9)
- [@GISMO_ai on X](https://twitter.com/GISMO_ai)

---

*Policy before power. No silent actions. Everything yours.*
