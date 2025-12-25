import tempfile
import unittest
from pathlib import Path

from gismo.cli import main as cli_main
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

    def test_daemon_subcommand_routes_to_daemon(self) -> None:
        parser = cli_main.build_parser()
        args = parser.parse_args(["daemon", "--once"])

        self.assertEqual(args.command, "daemon")
        self.assertIs(args.handler, cli_main._handle_daemon)
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

    def test_enqueue_and_daemon_share_db_path(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        policy_path = str(repo_root / "policy" / "readonly.json")
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = str(Path(tmpdir) / "state.db")

            cli_main.run_enqueue(db_path, "echo: systemd", run_id=None, max_attempts=1)
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


if __name__ == "__main__":
    unittest.main()
