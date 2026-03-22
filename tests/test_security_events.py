import shutil
import sqlite3
import unittest
from pathlib import Path
from uuid import uuid4

from gismo.core.security_events import append_security_event
from gismo.core.state import StateStore


class SecurityEventsTest(unittest.TestCase):
    def _tmpdir(self, label: str) -> Path:
        path = Path("tmp") / f"{label}-{uuid4().hex}"
        path.mkdir(parents=True, exist_ok=False)
        return path

    def test_chain_validation_detects_tampering(self) -> None:
        tmpdir = self._tmpdir("security-chain")
        try:
            db_path = str(tmpdir / "state.db")
            store = StateStore(db_path)
            append_security_event(
                db_path=db_path,
                event_type="quarantine_created",
                actor="test",
                action="create",
                resource="quarantine:1",
                payload={"value": 1},
            )
            append_security_event(
                db_path=db_path,
                event_type="trust_transition",
                actor="test",
                action="promote",
                resource="memory:global/example",
                payload={"value": 2},
            )
            status = store.validate_security_event_chain()
            self.assertTrue(status.valid)

            with sqlite3.connect(db_path) as connection:
                connection.execute(
                    "UPDATE security_events SET payload_json = ? WHERE seq = 1",
                    ('{"value":999}',),
                )
                connection.commit()

            tampered = store.validate_security_event_chain()
            self.assertFalse(tampered.valid)
            self.assertEqual(tampered.mismatch_seq, 1)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
