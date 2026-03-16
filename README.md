# GISMO

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey.svg)]()
[![GitHub Stars](https://img.shields.io/github/stars/GISMO-1/GISMO?style=social)](https://github.com/GISMO-1/GISMO/stargazers)

**General Intelligent System for Multiflow Operations**

A local-first, policy-controlled personal AI that runs entirely on your hardware. No cloud. No silent actions. Full audit trail. Yours.

Built by [Mike Burns](https://x.com/GISMO_ai).

---

## What is GISMO?

GISMO is an autonomous AI orchestration system designed to plan, execute, and manage tasks on your own machine — privately, transparently, and under your complete control.

Every AI assistant you use today runs on someone else's server. Your data passes through their infrastructure. You don't own it.

GISMO is different. It runs locally using [Ollama](https://ollama.com) for its language model, stores everything in a local SQLite database, and gates every action through an explicit operator policy. Nothing happens without your permission. Nothing happens silently.

**Core philosophy: Policy before power.**

---

## Features

**Local LLM Brain** — Ollama integration, model-agnostic, zero cloud dependency. GISMO includes a fine-tuned model identity with its own Modelfile.

**Durable Task Engine** — Queue, daemon, state machine, resume-safe execution with retry handling and failure retention.

**Full Audit Trail** — Every decision, plan, and action logged in tamper-evident JSONL format with cryptographic receipts.

**Deterministic Risk Classification** — Every plan rated LOW / MEDIUM / HIGH before anything executes. No surprises.

**Policy-Gated Everything** — Deny by default. Shell commands blocked unless explicitly allowlisted. Confirmation gates for anything above LOW risk.

**Persistent Memory** — SQLite-backed memory with namespaces, profiles, retention rules, snapshots, and tamper detection. Operator-controlled writes only.

**Interactive Plan Approval** — Defer plans for review. Inspect, edit, approve, or reject individual actions before execution via CLI or web UI.

**Web Dashboard** — Local browser UI at `127.0.0.1:7800` with Queue, Runs, Memory, Plans, Chat, and Settings tabs. Zero external web framework dependencies.

**Terminal Dashboard (TUI)** — Live terminal interface with queue, runs, and daemon status. Auto-refreshes every 3 seconds.

**Chat Interface** — Talk to GISMO through the web UI. GISMO responds using the local LLM and speaks responses aloud.

**Text-to-Speech** — 5 selectable voices via piper-tts. Models download on first use. Voice preference stored in memory. Configurable in web Settings.

**Leashed Autonomy** — Agent loop that iterates toward goals under strict guardrails. Plans, enqueues, and executes only through the same policy gates as everything else.

**Windows-First** — Built for Windows as the primary platform. Also runs on Linux and macOS.

---

## Quick Start

**Prerequisites:** Python 3.11+, [Ollama](https://ollama.com) installed and running.

```bash
# Clone
git clone https://github.com/GISMO-1/GISMO.git
cd GISMO

# Set up virtual environment
python -m venv .venv

# Activate (Windows PowerShell)
.venv\Scripts\Activate.ps1

# Activate (Linux/macOS)
source .venv/bin/activate

# Install
pip install -e .

# Verify
python scripts/verify.py

# Create GISMO's model identity in Ollama
ollama create gismo -f Modelfile

# Launch the web dashboard
gismo web
```

Open `http://127.0.0.1:7800` in your browser. You're running.

---

## Core Commands

```bash
# Run a command
gismo run "echo:Hello from GISMO"

# Ask GISMO to plan something
gismo ask "Summarize the last 10 queue failures" --dry-run

# Defer a plan for review before execution
gismo ask "Do X safely" --defer

# Review and approve plans
gismo plan list
gismo plan show PLAN_ID
gismo plan approve PLAN_ID
gismo plan reject PLAN_ID --reason "too risky"
gismo plan edit PLAN_ID --action 1 --cmd "echo:updated"

# Queue and daemon
gismo enqueue "note:remember this"
gismo up          # start daemon
gismo status      # check health
gismo down        # stop daemon

# Web dashboard
gismo web

# Terminal dashboard
gismo tui

# Voice
gismo tts speak "Hello from GISMO"
gismo tts voices list
gismo tts voices set en_GB-northern_english_male-medium
```

---

## Architecture

```
gismo/
  core/       Orchestration engine, queue, daemon, state store, models
  memory/     SQLite memory store, profiles, retention, summarization
  llm/        Ollama integration, planner, prompt engineering
  cli/        All CLI commands and argument parsing
  tts/        Piper-tts voice engine, preferences, voice registry
  web/        Local web dashboard (API, server, templates)
  tui/        Terminal UI dashboard
  tools/      Tool implementations (echo, note, shell, graph)

policy/       Security policy files (readonly, dev-safe)
data/         Training data for fine-tuning
notebooks/    Colab notebook for model fine-tuning
tests/        Comprehensive test coverage
docs/         Operator guide and handoff documentation
```

---

## Voice

GISMO speaks using [piper-tts](https://github.com/rhasspy/piper) with 5 selectable voices:

| Voice | Language | Quality |
|-------|----------|---------|
| Northern English Male (default) | en-GB | medium |
| Alan | en-GB | medium |
| Lessac | en-US | medium |
| Ryan | en-US | high |
| Amy | en-US | medium |

Voice models download automatically on first use and are cached locally. Configure in the web Settings tab or via `gismo tts voices set`.

---

## Policy & Safety

GISMO's safety model is built on real industrial robotics experience. The developer operates real industrial robots — he knows what happens when machines act without proper controls.

- **Deny by default** — nothing executes unless explicitly permitted
- **Deterministic risk classification** — LOW / MEDIUM / HIGH for every plan
- **Confirmation gates** — operator approval required for MEDIUM and HIGH risk
- **Full audit trail** — every action logged with cryptographic receipts
- **No silent actions** — ever

---

## Roadmap

| Phase | Status |
|-------|--------|
| Phase 0 — Foundation | ✅ Complete |
| Phase 1 — Local LLM Planner | ✅ Complete |
| Phase 2 — Control & Guardrails | ✅ Complete |
| Phase 3 — Memory & Context | ✅ Complete |
| Phase 4 — Interactive GISMO | 🔄 In Progress |

**Phase 4 completed so far:** TUI dashboard, web UI, TTS voice support, interactive plan approval, chat interface, fine-tuned model.

**Up next:** Always-on service behavior, standalone application packaging, authentication and security.

---

## The Story

GISMO was born on Christmas Day 2025. Built by a factory worker who operates industrial robots by day and codes by night, on a 7-year-old laptop in Auburn, New York.

The name is a nod to Gizmo from Gremlins — cute, friendly, helpful. But you need proper rules and controls to keep things safe. That's the whole point.

---

## Documentation

- [Operator Guide](docs/OPERATOR.md) — usage and lifecycle guidance
- [Handoff](Handoff.md) — maintainer handoff and architecture overview

---

## License

[MIT](LICENSE) — Free. Open source. Yours.

---

**GitHub:** [github.com/GISMO-1/GISMO](https://github.com/GISMO-1/GISMO)
**Twitter:** [@GISMO_ai](https://x.com/GISMO_ai)
