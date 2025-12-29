import json
import subprocess
import unittest
from unittest import mock

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

        def fake_urlopen(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return DummyResponse("{}")

        with (
            mock.patch.object(
                ollama.urllib.request,
                "urlopen",
                side_effect=fake_urlopen,
            ),
            mock.patch.object(ollama, "_extract_message_content", return_value="{}"),
        ):
            response = ollama.ollama_chat("ping", "return JSON")
        self.assertEqual(response, "{}")
        body = json.loads(captured["request"].data.decode("utf-8"))
        self.assertEqual(body["format"], "json")
        self.assertEqual(body["keep_alive"], ollama.DEFAULT_OLLAMA_KEEP_ALIVE)
        self.assertEqual(body["options"]["temperature"], 0)


class OllamaCurlTransportTest(unittest.TestCase):
    def test_curl_transport_writes_payload_and_invokes_curl(self) -> None:
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

        def fake_run(command, capture_output, text, check, timeout):
            captured["command"] = command
            captured["timeout"] = timeout
            data_arg = command[-1]
            self.assertTrue(data_arg.startswith("@"))
            with open(data_arg[1:], encoding="utf-8") as handle:
                body = json.load(handle)
            self.assertEqual(body["format"], "json")
            self.assertEqual(body["options"]["temperature"], 0)
            return subprocess.CompletedProcess(
                command,
                0,
                stdout='{"message":{"content":"{}"}}',
                stderr="",
            )

        with mock.patch.object(ollama.subprocess, "run", side_effect=fake_run):
            response = ollama._ollama_chat_via_curl(
                "http://127.0.0.1:11434/api/chat",
                payload_json,
                timeout_s=config.timeout_s,
                config=config,
                curl_executable="curl.exe",
            )
        self.assertEqual(response, "{}")
        self.assertIn("curl.exe", captured["command"][0])
        self.assertIn("--data-binary", captured["command"])
        self.assertEqual(captured["timeout"], 5)


if __name__ == "__main__":
    unittest.main()
