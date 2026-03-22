import json
import os
import subprocess
import unittest
from unittest import mock

from gismo.core.execution import build_execution_request
from gismo.llm import ollama


class DummyResponse:
    def __init__(self, body: str) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


class OllamaClientPayloadTest(unittest.TestCase):
    def test_chat_payload_includes_json_format_and_keep_alive(self) -> None:
        captured = {}

        def fake_run(command, **kwargs):
            captured["command"] = command
            captured["timeout"] = kwargs["timeout"]
            captured["worker_input"] = json.loads(kwargs["input"])
            captured["cwd"] = kwargs.get("cwd")
            captured["env"] = kwargs.get("env")
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    {
                        "ok": True,
                        "body": '{"message":{"content":"{}"}}',
                        "exit_code": 0,
                        "stdout": "",
                        "stderr": "",
                    }
                ),
                stderr="",
            )

        with (
            mock.patch.dict(os.environ, {"GISMO_OLLAMA_TRANSPORT": "python", "GISMO_TEST_SECRET": "hidden"}),
            mock.patch("gismo.core.execution.subprocess.run", side_effect=fake_run),
        ):
            response = ollama.ollama_chat("ping", "return JSON")

        self.assertEqual(response, "{}")
        self.assertEqual(captured["command"][:3], [os.sys.executable, "-m", "gismo.core.isolation_worker"])
        self.assertEqual(captured["command"][3], "ollama_python")
        self.assertIsNotNone(captured["cwd"])
        self.assertNotIn("GISMO_TEST_SECRET", captured["env"])

        body = json.loads(captured["worker_input"]["payload_json"])
        self.assertEqual(body["format"], "json")
        self.assertIn("keep_alive", body)
        self.assertEqual(body["options"]["temperature"], 0)


class OllamaCurlTransportTest(unittest.TestCase):
    def test_curl_transport_uses_isolated_worker_payload(self) -> None:
        captured = {}

        payload = ollama.build_ollama_chat_payload(
            "ping",
            "return JSON",
            model="phi3:mini",
        )
        payload_json = json.dumps(payload)

        config = ollama.OllamaConfig(
            url="http://127.0.0.1:11434",
            model="phi3:mini",
            timeout_s=5,
            transport="curl",
        )

        def fake_run(command, input, capture_output, text, encoding, timeout, check):
            captured["command"] = command
            captured["timeout"] = timeout
            captured["worker_input"] = json.loads(input)
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    {
                        "ok": True,
                        "stdout": '{"message":{"content":"{}"}}',
                        "stderr": "",
                        "exit_code": 0,
                    }
                ),
                stderr="",
            )

        with mock.patch("gismo.core.execution.subprocess.run", side_effect=fake_run):
            response = ollama._ollama_chat_via_curl(
                "http://127.0.0.1:11434/api/chat",
                payload_json,
                timeout_s=config.timeout_s,
                config=config,
                curl_executable="curl.exe",
                request=build_execution_request(
                    component="ollama_client",
                    action="model_request",
                    actor="test",
                    mode="isolated_subprocess",
                ),
            )

        self.assertEqual(response, "{}")
        self.assertEqual(captured["command"][:3], [os.sys.executable, "-m", "gismo.core.isolation_worker"])
        self.assertEqual(captured["command"][3], "curl")
        self.assertEqual(captured["worker_input"]["curl_executable"], "curl.exe")
        self.assertEqual(captured["worker_input"]["url"], "http://127.0.0.1:11434/api/chat")
        worker_payload = json.loads(captured["worker_input"]["payload_json"])
        self.assertEqual(worker_payload["format"], "json")
        self.assertEqual(worker_payload["options"]["temperature"], 0)
        self.assertEqual(captured["timeout"], 7.0)


if __name__ == "__main__":
    unittest.main()
