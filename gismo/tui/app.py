"""GISMO Terminal UI — live dashboard for queue, runs, and daemon status."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, Header, Static, TabbedContent, TabPane

from gismo.core.state import StateStore

# ── Constants ─────────────────────────────────────────────────────────────────

STALE_HEARTBEAT_SECONDS = 30
REFRESH_INTERVAL = 3.0
QUEUE_ITEM_LIMIT = 100
RUNS_LIMIT = 50


# ── Helpers ───────────────────────────────────────────────────────────────────


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _age_str(dt: datetime | None) -> str:
    if dt is None:
        return "-"
    secs = max(0, int((_utc_now() - _ensure_utc(dt)).total_seconds()))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    return f"{secs // 3600}h"


def _trunc(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


_STATUS_STYLE: dict[str, str] = {
    "QUEUED": "dim",
    "IN_PROGRESS": "bold yellow",
    "RUNNING": "bold yellow",
    "SUCCEEDED": "green",
    "FAILED": "bold red",
    "CANCELLED": "dim",
    "PENDING": "dim",
    "succeeded": "green",
    "failed": "bold red",
    "running": "bold yellow",
    "pending": "dim",
}


def _styled(status: str) -> str:
    s = _STATUS_STYLE.get(status, "")
    return f"[{s}]{status}[/{s}]" if s else status


def _status_val(status: Any) -> str:
    return status.value if hasattr(status, "value") else str(status)


# ── Sidebar widgets ───────────────────────────────────────────────────────────


class DaemonPanel(Static):
    """Sidebar panel: daemon health indicator."""

    DEFAULT_CSS = """
    DaemonPanel {
        height: auto;
        padding: 0 0 1 0;
    }
    """

    def refresh_from(self, db_path: str) -> None:
        try:
            with StateStore(db_path) as store:
                hb = store.get_daemon_heartbeat()
                paused = store.get_daemon_paused()
        except Exception:
            self.update("[bold]Daemon[/bold]\n[dim]unavailable[/dim]")
            return

        if hb is None:
            self.update("[bold]Daemon[/bold]\n○ [dim]not running[/dim]")
            return

        age_secs = max(0, int((_utc_now() - _ensure_utc(hb.last_seen)).total_seconds()))
        stale = age_secs > STALE_HEARTBEAT_SECONDS

        dot = "● [bold red]stale[/bold red]" if stale else "● [bold green]running[/bold green]"
        pause_str = "[bold yellow]paused[/bold yellow]" if paused else "[green]active[/green]"

        self.update(
            f"[bold]Daemon[/bold]\n"
            f"{dot}\n"
            f"  {pause_str}\n"
            f"  pid {hb.pid}\n"
            f"  hb {age_secs}s ago"
        )


class QueueStatsPanel(Static):
    """Sidebar panel: queue item counts by status."""

    DEFAULT_CSS = """
    QueueStatsPanel {
        height: auto;
    }
    """

    def refresh_from(self, db_path: str) -> None:
        try:
            with StateStore(db_path) as store:
                stats = store.queue_stats()
        except Exception:
            self.update("[bold]Queue[/bold]\n[dim]unavailable[/dim]")
            return

        by = stats.get("by_status", {})
        total = stats.get("total", 0)
        q = by.get("QUEUED", 0)
        ip = by.get("IN_PROGRESS", 0)
        s = by.get("SUCCEEDED", 0)
        f = by.get("FAILED", 0)
        c = by.get("CANCELLED", 0)

        ip_str = f"[bold yellow]{ip}[/bold yellow]" if ip else str(ip)
        f_str = f"[bold red]{f}[/bold red]" if f else str(f)

        self.update(
            f"[bold]Queue[/bold] ({total})\n"
            f"  queued      {q}\n"
            f"  in-progress {ip_str}\n"
            f"  succeeded   {s}\n"
            f"  failed      {f_str}\n"
            f"  cancelled   {c}"
        )


# ── Main App ──────────────────────────────────────────────────────────────────


class GismoApp(App[None]):
    """GISMO live dashboard — queue, runs, and daemon status."""

    CSS = """
    Screen {
        layout: horizontal;
    }

    #sidebar {
        width: 26;
        height: 100%;
        border-right: solid $panel;
        padding: 0 1;
        overflow-y: auto;
    }

    #sidebar-sep {
        height: 1;
        border-bottom: dashed $panel;
        margin-bottom: 1;
    }

    #main {
        width: 1fr;
        height: 100%;
    }

    TabbedContent {
        height: 1fr;
    }

    TabPane {
        height: 1fr;
        padding: 0;
    }

    DataTable {
        height: 1fr;
    }

    #daemon-detail {
        padding: 1 2;
        height: 1fr;
        overflow-y: auto;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "force_refresh", "Refresh"),
        Binding("p", "toggle_pause", "Pause/Resume"),
    ]

    TITLE = "GISMO"

    def __init__(self, db_path: str) -> None:
        super().__init__()
        self._db_path = db_path
        self.sub_title = str(Path(db_path).resolve())

    # ── Layout ────────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            with Vertical(id="sidebar"):
                yield DaemonPanel(id="daemon-panel")
                yield Static(id="sidebar-sep")
                yield QueueStatsPanel(id="queue-stats-panel")
            with Vertical(id="main"):
                with TabbedContent(initial="tab-queue"):
                    with TabPane("Queue", id="tab-queue"):
                        yield DataTable(id="queue-table", cursor_type="row")
                    with TabPane("Runs", id="tab-runs"):
                        yield DataTable(id="runs-table", cursor_type="row")
                    with TabPane("Daemon", id="tab-daemon"):
                        yield Static(id="daemon-detail", expand=True)
        yield Footer()

    def on_mount(self) -> None:
        self._setup_queue_table()
        self._setup_runs_table()
        self._refresh_all()
        self.set_interval(REFRESH_INTERVAL, self._refresh_all)

    # ── Table column setup ────────────────────────────────────────────────────

    def _setup_queue_table(self) -> None:
        t = self.query_one("#queue-table", DataTable)
        t.add_column("ID", width=10, key="id")
        t.add_column("Status", width=14, key="status")
        t.add_column("Att", width=6, key="att")
        t.add_column("Age", width=6, key="age")
        t.add_column("Command", key="cmd")

    def _setup_runs_table(self) -> None:
        t = self.query_one("#runs-table", DataTable)
        t.add_column("ID", width=10, key="id")
        t.add_column("Status", width=11, key="status")
        t.add_column("Tasks", width=12, key="tasks")
        t.add_column("Age", width=6, key="age")
        t.add_column("Label / Error", key="label")

    # ── Refresh ───────────────────────────────────────────────────────────────

    def _refresh_all(self) -> None:
        self._refresh_sidebar()
        self._refresh_queue_table()
        self._refresh_runs_table()
        self._refresh_daemon_detail()

    def _refresh_sidebar(self) -> None:
        self.query_one("#daemon-panel", DaemonPanel).refresh_from(self._db_path)
        self.query_one("#queue-stats-panel", QueueStatsPanel).refresh_from(self._db_path)

    def _refresh_queue_table(self) -> None:
        table = self.query_one("#queue-table", DataTable)
        table.clear()
        try:
            with StateStore(self._db_path) as store:
                items = store.list_queue_items(limit=QUEUE_ITEM_LIMIT, newest_first=True)
        except Exception:
            return
        for item in items:
            status = _status_val(item.status)
            table.add_row(
                item.id[:8],
                _styled(status),
                f"{item.attempt_count}/{item.max_retries + 1}",
                _age_str(item.created_at),
                _trunc(item.command_text, 70),
            )

    def _refresh_runs_table(self) -> None:
        table = self.query_one("#runs-table", DataTable)
        table.clear()
        try:
            with StateStore(self._db_path) as store:
                runs = list(store.list_runs(limit=RUNS_LIMIT, newest_first=True))
                task_map = {run.id: list(store.list_tasks(run.id)) for run in runs}
        except Exception:
            return
        for run in runs:
            tasks = task_map.get(run.id, [])
            statuses = [_status_val(t.status) for t in tasks]
            total = len(tasks)
            succ = statuses.count("SUCCEEDED")
            fail = statuses.count("FAILED")
            running = statuses.count("RUNNING")

            if fail:
                run_status = "failed"
            elif running:
                run_status = "running"
            elif total and succ == total:
                run_status = "succeeded"
            else:
                run_status = "pending"

            label_col = run.label or "-"
            if fail:
                ft = next((t for t in tasks if _status_val(t.status) == "FAILED"), None)
                if ft and ft.error:
                    label_col = _trunc(ft.error, 50)

            table.add_row(
                run.id[:8],
                _styled(run_status),
                f"{total} ✓{succ} ✗{fail}",
                _age_str(run.created_at),
                label_col,
            )

    def _refresh_daemon_detail(self) -> None:
        panel = self.query_one("#daemon-detail", Static)
        try:
            with StateStore(self._db_path) as store:
                hb = store.get_daemon_heartbeat()
                paused = store.get_daemon_paused()
        except Exception:
            panel.update("[bold red]Cannot read daemon state[/bold red]")
            return

        if hb is None:
            panel.update(
                "[dim]Daemon is not running.[/dim]\n\n"
                "Start with: [bold]gismo up[/bold]"
            )
            return

        last_seen = _ensure_utc(hb.last_seen)
        started = _ensure_utc(hb.started_at)
        age_secs = max(0, int((_utc_now() - last_seen).total_seconds()))
        stale = age_secs > STALE_HEARTBEAT_SECONDS

        health = "[bold red]STALE[/bold red]" if stale else "[bold green]HEALTHY[/bold green]"
        ctrl = "[bold yellow]PAUSED[/bold yellow]" if paused else "[green]ACTIVE[/green]"

        panel.update(
            f"[bold]Daemon Status[/bold]\n\n"
            f"Health:    {health}\n"
            f"Control:   {ctrl}\n"
            f"PID:       {hb.pid}\n"
            f"Started:   {started.strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
            f"Last seen: {last_seen.strftime('%Y-%m-%d %H:%M:%S')} UTC  ({age_secs}s ago)\n\n"
            f"[dim]Press [bold]p[/bold] to pause / resume[/dim]"
        )

    # ── Actions ───────────────────────────────────────────────────────────────

    def action_force_refresh(self) -> None:
        self._refresh_all()
        self.notify("Refreshed", timeout=1)

    def action_toggle_pause(self) -> None:
        try:
            with StateStore(self._db_path) as store:
                paused = store.get_daemon_paused()
                store.set_daemon_paused(not paused)
            verb = "Paused" if not paused else "Resumed"
            self.notify(f"Daemon {verb}", timeout=2)
            self._refresh_all()
        except Exception as exc:
            self.notify(f"Error: {exc}", severity="error", timeout=3)


def run(db_path: str) -> None:
    """Entry point: launch the TUI."""
    GismoApp(db_path=db_path).run()
