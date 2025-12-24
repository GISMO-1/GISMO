import unittest

from gismo.cli import main as cli_main


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


if __name__ == "__main__":
    unittest.main()
