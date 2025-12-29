import json
import os
import unittest

from gismo.llm.ollama import ollama_chat, resolve_ollama_config
from gismo.llm.prompts import build_system_prompt, build_user_prompt


@unittest.skipUnless(
    os.getenv("GISMO_TEST_INTEGRATION_OLLAMA") == "1",
    "Set GISMO_TEST_INTEGRATION_OLLAMA=1 to run Ollama integration tests.",
)
class OllamaIntegrationTest(unittest.TestCase):
    def test_ollama_chat_round_trip(self) -> None:
        config = resolve_ollama_config()
        response = ollama_chat(
            build_user_prompt("ping"),
            build_system_prompt(),
            model=config.model,
            host=config.url,
            timeout_s=config.timeout_s,
        )
        parsed = json.loads(response)
        self.assertIsInstance(parsed, dict)
        self.assertIn("intent", parsed)


if __name__ == "__main__":
    unittest.main()
