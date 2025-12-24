import tempfile
import unittest
from pathlib import Path

from gismo.core.toolpacks.fs_tools import FileSystemConfig, ListDirTool, ReadFileTool, WriteFileTool
from gismo.core.toolpacks.shell_tool import ShellConfig, ShellTool


class FileSystemToolTest(unittest.TestCase):
    def test_fs_tools_respect_base_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir) / "base"
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


class ShellToolTest(unittest.TestCase):
    def test_shell_tool_denies_non_allowlisted_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            config = ShellConfig(base_dir=base_dir, allowlist=[["echo", "ok"]], timeout_seconds=2)
            tool = ShellTool(config)

            with self.assertRaises(PermissionError):
                tool.run({"command": ["ls"]})

    def test_shell_tool_logs_output_and_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            config = ShellConfig(base_dir=base_dir, allowlist=[["echo", "hello"]], timeout_seconds=2)
            tool = ShellTool(config)

            result = tool.run({"command": ["echo", "hello"]})
            self.assertEqual(result["stdout"].strip(), "hello")
            self.assertEqual(result["stderr"], "")
            self.assertEqual(result["exit_code"], 0)

            with self.assertRaises(PermissionError):
                tool.run({"command": ["echo", "hello"], "cwd": "../outside"})


if __name__ == "__main__":
    unittest.main()
