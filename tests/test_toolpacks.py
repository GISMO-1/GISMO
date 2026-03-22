import unittest
from pathlib import Path
from uuid import uuid4
import shutil

from gismo.core.toolpacks.fs_tools import FileSystemConfig, ListDirTool, ReadFileTool, WriteFileTool
from gismo.core.toolpacks.shell_tool import ShellConfig, ShellTool


def _tmpdir(label: str) -> Path:
    path = Path("tmp") / f"{label}-{uuid4().hex}"
    path.mkdir(parents=True, exist_ok=False)
    return path


class FileSystemToolTest(unittest.TestCase):
    def test_fs_tools_respect_base_dir(self) -> None:
        tmpdir = _tmpdir("toolpacks-fs")
        try:
            base_dir = tmpdir / "base"
            base_dir.mkdir()
            config = FileSystemConfig(base_dir=base_dir)
            read_tool = ReadFileTool(config)
            write_tool = WriteFileTool(config)
            list_tool = ListDirTool(config)

            write_result = write_tool.run({"path": "notes/hello.txt", "content": "hello"})
            self.assertTrue(write_result["bytes_written"] > 0)

            read_result = read_tool.run({"path": "notes/hello.txt"})
            self.assertEqual(read_result["content"], "hello")

            list_result = list_tool.run({"path": "notes"})
            self.assertIn("hello.txt", list_result["entries"])

            with self.assertRaises(PermissionError):
                read_tool.run({"path": "../outside.txt"})
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class ShellToolTest(unittest.TestCase):
    def test_shell_tool_denies_non_allowlisted_command(self) -> None:
        tmpdir = _tmpdir("toolpacks-shell-deny")
        try:
            base_dir = tmpdir
            config = ShellConfig(base_dir=base_dir, allowlist=[["echo", "ok"]], timeout_seconds=2)
            tool = ShellTool(config)

            with self.assertRaises(PermissionError):
                tool.run({"command": ["ls"]})
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_shell_tool_logs_output_and_exit_code(self) -> None:
        tmpdir = _tmpdir("toolpacks-shell-run")
        try:
            base_dir = tmpdir
            config = ShellConfig(base_dir=base_dir, allowlist=[["echo", "hello"]], timeout_seconds=2)
            tool = ShellTool(config)

            result = tool.run({"command": ["echo", "hello"]})
            self.assertEqual(result["stdout"].strip(), "hello")
            self.assertEqual(result["stderr"], "")
            self.assertEqual(result["exit_code"], 0)
            self.assertEqual(result["execution"]["mode"], "isolated_subprocess")

            with self.assertRaises(PermissionError):
                tool.run({"command": ["echo", "hello"], "cwd": "../outside"})
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
