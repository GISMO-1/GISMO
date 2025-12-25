import tempfile
import unittest
from pathlib import Path

from gismo.cli import windows_startup


class WindowsStartupTest(unittest.TestCase):
    def test_get_windows_startup_folder_uses_appdata(self) -> None:
        base = "C:\\Users\\alice\\AppData\\Roaming"
        path = windows_startup.get_windows_startup_folder(appdata=base)
        self.assertEqual(
            path,
            Path(base)
            / "Microsoft"
            / "Windows"
            / "Start Menu"
            / "Programs"
            / "Startup",
        )

    def test_build_launcher_content_quotes_paths(self) -> None:
        content = windows_startup.build_windows_startup_launcher_content(
            "C:\\Program Files\\Python\\python.exe",
            "C:\\Users\\Alice\\GISMO State\\state.db",
        )
        expected = (
            "@echo off\n"
            "\"C:\\Program Files\\Python\\python.exe\" -m gismo.cli.main daemon "
            "--db \"C:\\Users\\Alice\\GISMO State\\state.db\"\n"
        )
        self.assertEqual(content, expected)

    def test_install_does_not_overwrite_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            startup_dir = Path(temp_dir)
            launcher = startup_dir / "GISMO Daemon.cmd"
            launcher.write_text("original\n", encoding="utf-8")
            windows_startup.install_windows_startup_launcher(
                name="GISMO Daemon",
                db_path="state.db",
                python_exe="python.exe",
                force=False,
                startup_dir=startup_dir,
            )
            self.assertEqual(launcher.read_text(encoding="utf-8"), "original\n")

    def test_uninstall_requires_yes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            startup_dir = Path(temp_dir)
            launcher = startup_dir / "GISMO Daemon.cmd"
            launcher.write_text("content\n", encoding="utf-8")
            windows_startup.uninstall_windows_startup_launcher(
                name="GISMO Daemon",
                yes=False,
                startup_dir=startup_dir,
            )
            self.assertTrue(launcher.exists())
            windows_startup.uninstall_windows_startup_launcher(
                name="GISMO Daemon",
                yes=True,
                startup_dir=startup_dir,
            )
            self.assertFalse(launcher.exists())


if __name__ == "__main__":
    unittest.main()
