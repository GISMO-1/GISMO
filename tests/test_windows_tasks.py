import unittest

from gismo.cli import windows_tasks


class WindowsTasksCommandTest(unittest.TestCase):
    def test_build_daemon_command(self) -> None:
        command = windows_tasks.build_daemon_command("py.exe", "state.db")
        self.assertEqual(
            command,
            ["py.exe", "-m", "gismo.cli.main", "daemon", "--db", "state.db"],
        )

    def test_build_schtasks_create_args(self) -> None:
        args = windows_tasks.build_schtasks_create_args("GISMO Daemon", "task.xml", False)
        self.assertEqual(
            args,
            ["schtasks.exe", "/Create", "/TN", "GISMO Daemon", "/XML", "task.xml"],
        )

    def test_build_schtasks_create_args_with_force(self) -> None:
        args = windows_tasks.build_schtasks_create_args("GISMO Daemon", "task.xml", True)
        self.assertEqual(
            args,
            ["schtasks.exe", "/Create", "/TN", "GISMO Daemon", "/XML", "task.xml", "/F"],
        )

    def test_build_schtasks_delete_args(self) -> None:
        args = windows_tasks.build_schtasks_delete_args("GISMO Daemon")
        self.assertEqual(
            args,
            ["schtasks.exe", "/Delete", "/TN", "GISMO Daemon", "/F"],
        )

    def test_build_task_xml_includes_triggers_and_restart(self) -> None:
        command = windows_tasks.build_daemon_command("C:\\Python\\python.exe", "state.db")
        xml = windows_tasks.build_task_xml(command, "ACME\\operator")
        self.assertIn("<BootTrigger>", xml)
        self.assertIn("<LogonTrigger>", xml)
        self.assertIn("<RestartOnFailure>", xml)
        self.assertIn("C:\\Python\\python.exe", xml)
        self.assertIn("-m gismo.cli.main daemon --db state.db", xml)


if __name__ == "__main__":
    unittest.main()
