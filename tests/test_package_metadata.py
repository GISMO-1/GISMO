import tomllib
import unittest
from pathlib import Path


class PackageMetadataTest(unittest.TestCase):
    def test_console_script_entrypoint_is_defined(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        pyproject_path = repo_root / "pyproject.toml"

        with pyproject_path.open("rb") as handle:
            pyproject = tomllib.load(handle)

        scripts = pyproject.get("project", {}).get("scripts", {})
        self.assertEqual(scripts.get("gismo"), "gismo.cli.main:main")


if __name__ == "__main__":
    unittest.main()
