import json
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
            return DummyResponse('{"message":{"content":"{}"}}')

        with mock.patch.object(ollama.urllib.request, "urlopen", side_effect=fake_urlopen):
            response = ollama.ollama_chat("ping", "return JSON")
        self.assertEqual(response, "{}")
        body = json.loads(captured["request"].data.decode("utf-8"))
        self.assertEqual(body["format"], "json")
        self.assertEqual(body["keep_alive"], ollama.DEFAULT_OLLAMA_KEEP_ALIVE)
        self.assertEqual(body["options"]["temperature"], 0)


if __name__ == "__main__":
    unittest.main()
