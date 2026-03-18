# GISMO


[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey.svg)]()
[![GitHub Stars](https://img.shields.io/github/stars/GISMO-1/GISMO?style=social)](https://github.com/GISMO-1/GISMO/stargazers)

**Your personal AI that runs on your hardware.**

GISMO (General Intelligent System for Multiflow Operations) is a local-first personal AI assistant that lives on your computer. It manages your tasks, controls your connected devices, speaks to you by voice, and keeps everything completely private. No cloud. No subscription. No company listening in.

Built by Mike Burns. Open source. Free forever.

---

## What can GISMO do?

- **Talk to you** — chat by text or voice, get briefings, ask questions
- **Control your devices** — smart lights, cameras, sensors, thermostats (expanding)
- **Manage your tasks** — plan, queue, schedule, and track anything
- **Remember things** — persistent memory that learns your preferences over time
- **Monitor your home** — camera feeds, system health, activity alerts
- **Work offline** — runs entirely on your machine, no internet required
- **Speak aloud** — 11 high-quality voices powered by Kokoro TTS

---

## Quick Start

### Requirements
- Python 3.11+
- Windows 10/11 (primary), Linux/macOS also supported
- [Ollama](https://ollama.ai) for the local AI brain

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

**Desktop app** (recommended):
```bash
gismo app
```

**Web dashboard**:
```bash
gismo web
```

**Command line**:
```bash
gismo ask "What can you do?"
```

On first launch, GISMO walks you through setup — your name, preferred voice, and you're ready to go.

---

## The Command Center

GISMO opens as a mission control dashboard:

- **Center** — Chat with GISMO, the primary interface
- **Left panel** — Connected devices with live status
- **Right panel** — Activity feed and task queue
- **Top bar** — System status, search, your name

Talk naturally: *"Turn on the living room lights"*, *"Check the front door camera"*, *"What's my schedule today?"*

---

## Voice

GISMO speaks with 11 high-quality voices powered by Kokoro TTS. Choose your favorite during setup or change it anytime in Settings. Default voice: Lewis (British male, calm and clear).

---

## Privacy

Everything stays on your machine. GISMO doesn't phone home, doesn't collect analytics, and doesn't send your data anywhere. Your conversations, your device data, your preferences — all stored locally in a SQLite database that only you can access.

---

## Device Support

GISMO connects to devices on your local network:

| Device Type | Protocol | Status |
|---|---|---|
| Tapo cameras | RTSP / pytapo | In progress |
| FEIT / Tuya smart lights | Tuya local API | In progress |
| Generic IP cameras | RTSP | In progress |
| MQTT devices | MQTT | Planned |
| Zigbee / Z-Wave | Via bridges | Planned |
| Arduino / Raspberry Pi | Serial / GPIO | Planned |
| Home Assistant | REST API | Planned |

More devices added regularly. If it's on your network, GISMO can probably talk to it.

---

## How It Works

GISMO runs a local AI model (via Ollama) on your hardware. When you ask it to do something:

1. GISMO understands your request
2. Creates a plan with specific actions
3. Checks the plan against your safety rules
4. Executes with your approval (or automatically for safe actions)
5. Logs everything so you can review what happened

Under the hood: Python, SQLite, Ollama, Kokoro TTS, pywebview. No frameworks, no heavy dependencies.

---

## For Developers

GISMO has a full CLI and powerful internals:

```bash
gismo run "echo:Hello"          # Run a command immediately
gismo enqueue "note:remember"   # Queue for daemon execution
gismo ask "plan something"      # AI-powered planning
gismo agent "do X" --once       # Autonomous agent loop
gismo tts speak "Hello"         # Text-to-speech
gismo tui                       # Terminal dashboard
gismo daemon                    # Background task executor
gismo queue stats               # Queue inspection
gismo export --latest           # Audit log export
```

Architecture overview:
- `gismo/core/` — orchestration engine, queue, daemon
- `gismo/memory/` — SQLite memory store, profiles, retention
- `gismo/llm/` — Ollama integration, planner
- `gismo/tts/` — Kokoro + Piper text-to-speech
- `gismo/web/` — Command center dashboard and API
- `gismo/desktop/` — Native desktop app (pywebview)
- `gismo/cli/` — All CLI commands
- `tests/` — Test coverage

Full developer docs: [docs/OPERATOR.md](docs/OPERATOR.md)

---

## Roadmap

- [x] Foundation — queue, daemon, SQLite state, audit logging
- [x] Local LLM planner — Ollama integration, plan/approve/execute
- [x] Safety model — risk classification, policy gates, confirmation
- [x] Memory — persistent context, profiles, snapshots
- [x] Voice — Kokoro TTS with 11 voices
- [x] Command center — desktop app with mission control dashboard
- [x] Onboarding — first-run setup, personalized experience
- [x] Fine-tuned model — custom GISMO personality
- [ ] Device connections — cameras, lights, sensors, thermostats
- [ ] Network scanning — auto-discover devices on your network
- [ ] Camera feeds — live thumbnails and fullscreen viewer
- [ ] Smart home control — lights, locks, climate via chat
- [ ] Always-on service — auto-start, background operation
- [ ] Remote access — check on things from your phone
- [ ] Standalone installer — one-click setup for non-developers

---

## Why GISMO?

Every AI assistant today sends your data to a company's servers. Alexa listens for Amazon. Siri processes through Apple. Google Assistant feeds Google.

GISMO is different. It runs on YOUR computer. Nothing leaves. No subscription. No company can change the terms, shut it down, or sell your data. You own it completely.

The name comes from Gizmo in Gremlins — friendly and helpful, but with rules to keep things safe. That's GISMO's philosophy: powerful but controlled. Your AI, your rules.

---

## Support the Project

- Star this repo
- [Sponsor on GitHub](https://github.com/sponsors/GISMO-1)
- Report bugs and request features via [Issues](https://github.com/GISMO-1/GISMO/issues)
- Follow [@GISMO_ai](https://twitter.com/GISMO_ai) on X

---

## License

MIT License — free to use, modify, and distribute.

Built with determination on a 7-year-old laptop by [Mike Burns](https://github.com/GISMO-1).

Born December 25, 2025. 🎄
