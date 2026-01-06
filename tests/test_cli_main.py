import argparse
import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gismo.cli import main as cli_main
from gismo.cli import ipc as ipc_cli
from gismo.core.models import QueueStatus
from gismo.core.state import StateStore


class CliMainParserTest(unittest.TestCase):
    def test_run_subcommand_routes_to_operator(self) -> None:
        parser = cli_main.build_parser()
        args = parser.parse_args(["run", "echo:", "smoke"])

        self.assertEqual(args.command, "run")
        self.assertIs(args.handler, cli_main._handle_run)
        self.assertEqual(args.operator_command, ["echo:", "smoke"])

    def test_export_subcommand_routes_to_export(self) -> None:
        parser = cli_main.build_parser()
        args = parser.parse_args(["export", "--latest", "--format", "jsonl"])

        self.assertEqual(args.command, "export")
        self.assertIs(args.handler, cli_main._handle_export)
        self.assertTrue(args.latest)
        self.assertEqual(args.format, "jsonl")

    def test_export_accepts_positional_run_id(self) -> None:
        parser = cli_main.build_parser()
        run_id = "11111111-1111-1111-1111-111111111111"

        args = parser.parse_args(["export", run_id])

        self.assertEqual(args.command, "export")
        self.assertIs(args.handler, cli_main._handle_export)
        self.assertIsNone(args.run_id)
        self.assertEqual(args.run_id_arg, run_id)

    def test_demo_subcommand_routes_to_demo(self) -> None:
        parser = cli_main.build_parser()
        args = parser.parse_args(["demo"])

        self.assertEqual(args.command, "demo")
        self.assertIs(args.handler, cli_main._handle_demo)

    def test_enqueue_subcommand_routes_to_enqueue(self) -> None:
        parser = cli_main.build_parser()
        args = parser.parse_args(["enqueue", "echo:", "hello"])

        self.assertEqual(args.command, "enqueue")
        self.assertIs(args.handler, cli_main._handle_enqueue)
        self.assertEqual(args.operator_command, ["echo:", "hello"])

    def test_ask_subcommand_routes_to_ask(self) -> None:
        parser = cli_main.build_parser()
        args = parser.parse_args(["ask", "draft", "plan"])

        self.assertEqual(args.command, "ask")
        self.assertIs(args.handler, cli_main._handle_ask)
        self.assertEqual(args.text, ["draft", "plan"])

    def test_agent_subcommand_routes_to_agent(self) -> None:
        parser = cli_main.build_parser()
        args = parser.parse_args(["agent", "do", "thing"])

        self.assertEqual(args.command, "agent")
        self.assertIs(args.handler, cli_main._handle_agent)
        self.assertEqual(args.goal, ["do", "thing"])

    def test_agent_session_subcommand_routes(self) -> None:
        parser = cli_main.build_parser()
        args = parser.parse_args(["agent-session", "list"])

        self.assertEqual(args.command, "agent-session")
        self.assertIs(args.handler, cli_main._handle_agent_session_list)

    def test_daemon_subcommand_routes_to_daemon(self) -> None:
        parser = cli_main.build_parser()
        args = parser.parse_args(["daemon", "--once"])

        self.assertEqual(args.command, "daemon")
        self.assertIs(args.handler, cli_main._handle_daemon)
        self.assertTrue(args.once)

    def test_maintain_subcommand_routes_to_maintain(self) -> None:
        parser = cli_main.build_parser()
        args = parser.parse_args(["maintain", "--once"])

        self.assertEqual(args.command, "maintain")
        self.assertIs(args.handler, cli_main._handle_maintain)
        self.assertTrue(args.once)

    def test_ipc_db_path_before_command(self) -> None:
        parser = cli_main.build_parser()
        db_path = "custom.db"

        args = parser.parse_args(["--db", db_path, "ipc", "queue-stats"])

        self.assertEqual(args.command, "ipc")
        self.assertEqual(args.ipc_command, "queue-stats")
        self.assertEqual(args.db_path, db_path)

    def test_ipc_db_path_after_subcommand(self) -> None:
        parser = cli_main.build_parser()
        db_path = "custom.db"

        enqueue_args = parser.parse_args(
            ["ipc", "enqueue", "--db", db_path, "echo:", "hello"]
        )
        self.assertEqual(enqueue_args.db_path, db_path)

        queue_stats_args = parser.parse_args(["ipc", "queue-stats", "--db", db_path])
        self.assertEqual(queue_stats_args.db_path, db_path)

        run_show_args = parser.parse_args(["ipc", "run-show", "--db", db_path, "run-1"])
        self.assertEqual(run_show_args.db_path, db_path)

        serve_args = parser.parse_args(["ipc", "serve", "--db", db_path])
        self.assertEqual(serve_args.db_path, db_path)

    def test_db_path_before_and_after_subcommand(self) -> None:
        parser = cli_main.build_parser()
        db_path = "custom.db"

        queue_before = parser.parse_args(["--db", db_path, "queue", "stats"])
        self.assertEqual(queue_before.db_path, db_path)

        queue_after = parser.parse_args(["queue", "stats", "--db", db_path])
        self.assertEqual(queue_after.db_path, db_path)

        run_before = parser.parse_args(["--db", db_path, "run", "echo:", "hi"])
        self.assertEqual(run_before.db_path, db_path)

        run_after = parser.parse_args(["run", "--db", db_path, "echo:", "hi"])
        self.assertEqual(run_after.db_path, db_path)

        export_before = parser.parse_args(["--db", db_path, "export", "--latest"])
        self.assertEqual(export_before.db_path, db_path)

        export_after = parser.parse_args(["export", "--latest", "--db", db_path])
        self.assertEqual(export_after.db_path, db_path)

    def test_supervise_subcommand_routes_to_supervise(self) -> None:
        parser = cli_main.build_parser()
        args = parser.parse_args(["supervise", "status", "--token", "token"])

        self.assertEqual(args.command, "supervise")
        self.assertEqual(args.supervise_command, "status")
        self.assertIs(args.handler, cli_main._handle_supervise_status)

    def test_supervise_aliases_route_to_handlers(self) -> None:
        parser = cli_main.build_parser()

        up_args = parser.parse_args(["up", "--token", "token"])
        self.assertEqual(up_args.command, "up")
        self.assertIs(up_args.handler, cli_main._handle_supervise_up)

        status_args = parser.parse_args(["status", "--token", "token"])
        self.assertEqual(status_args.command, "status")
        self.assertIs(status_args.handler, cli_main._handle_supervise_status)

        down_args = parser.parse_args(["down"])
        self.assertEqual(down_args.command, "down")
        self.assertIs(down_args.handler, cli_main._handle_supervise_down)

    def test_supervise_status_uses_env_token_fallback(self) -> None:
        args = argparse.Namespace(token=None, db_path="state.db")
        with mock.patch.dict(os.environ, {"GISMO_IPC_TOKEN": "env-token"}, clear=False):
            with mock.patch.object(
                cli_main.supervise_cli, "run_supervise_status"
            ) as run_status:
                cli_main._handle_supervise_status(args)
                run_status.assert_called_once_with("env-token", db_path="state.db")

    def test_recover_routes_to_handler(self) -> None:
        parser = cli_main.build_parser()
        args = parser.parse_args(["recover"])

        self.assertEqual(args.command, "recover")
        self.assertIs(args.handler, cli_main._handle_recover)

    def test_enqueue_and_daemon_share_db_path(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        policy_path = str(repo_root / "policy" / "readonly.json")
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "state.db")

            cli_main.run_enqueue(
                db_path,
                "echo: systemd",
                run_id=None,
                max_retries=1,
                timeout_seconds=300,
            )
            cli_main.run_daemon(
                db_path,
                policy_path,
                sleep_seconds=0.0,
                once=True,
                requeue_stale_seconds=600,
            )

            state_store = StateStore(db_path)
            run = state_store.get_latest_run()
            assert run is not None
            queue_item_id = run.metadata_json["queue_item_id"]
            item = state_store.get_queue_item(queue_item_id)
            assert item is not None
            self.assertEqual(item.status, QueueStatus.SUCCEEDED)

    def test_export_anchors_default_output_to_db_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir) / "repo"
            db_path = repo_root / ".gismo" / "state.db"
            state_store = StateStore(str(db_path))
            run = state_store.create_run(label="export", metadata={})
            other_cwd = Path(tmpdir) / "cwd"
            other_cwd.mkdir(parents=True, exist_ok=True)

            original_cwd = os.getcwd()
            try:
                os.chdir(other_cwd)
                cli_main.run_export(
                    str(db_path),
                    run_id=run.id,
                    use_latest=False,
                    export_format="jsonl",
                    out_path=None,
                    redact=False,
                    policy_path=None,
                )
            finally:
                os.chdir(original_cwd)

            expected = repo_root / "exports" / f"{run.id}.jsonl"
            self.assertTrue(expected.exists())
            self.assertFalse((other_cwd / "exports" / f"{run.id}.jsonl").exists())

    def test_ipc_queue_stats_connection_error(self) -> None:
        args = argparse.Namespace(token="secret-token")
        with mock.patch.object(
            ipc_cli,
            "ipc_request",
            side_effect=ipc_cli.IPCConnectionError("connection failed"),
        ):
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                with self.assertRaises(SystemExit) as exc:
                    cli_main._handle_ipc_queue_stats(args)
            self.assertEqual(exc.exception.code, 2)
            output = buffer.getvalue().strip().splitlines()
            self.assertEqual(
                output[0],
                "IPC server unreachable. Start it with: "
                "python -m gismo.cli.main ipc serve --db .gismo/state.db "
                "or run: python -m gismo.cli.main supervise up --db .gismo/state.db",
            )
            self.assertEqual(
                output[1],
                "Ensure GISMO_IPC_TOKEN matches on server and client.",
            )

    @unittest.skipIf(os.name != "nt", "Windows-only handle release check")
    def test_queue_stats_releases_db_handle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "locktest.db"
            argv = ["gismo", "queue", "stats", "--db", str(db_path)]

            with mock.patch.object(sys, "argv", argv):
                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    cli_main.main()

            self.assertTrue(db_path.exists())
            renamed_path = db_path.with_name("locktest-renamed.db")
            os.replace(db_path, renamed_path)
            os.remove(renamed_path)

    def test_prompt_paste_guard_exits(self) -> None:
        argv = [
            "gismo",
            "(.venv)",
            "PS",
            "D:\\work>",
            "python",
            "-m",
            "gismo.cli.main",
            "queue",
            "stats",
        ]
        with mock.patch.object(sys, "argv", argv):
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                with self.assertRaises(SystemExit) as exc:
                    cli_main.main()
        self.assertEqual(exc.exception.code, 2)
        self.assertIn("It looks like you pasted your shell prompt.", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
