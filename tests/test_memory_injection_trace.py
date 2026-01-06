import unittest

from gismo.memory.injection import injection_hash_for_items, order_memory_items
from gismo.memory.store import MemoryItem


def _memory_item(
    item_id: str,
    *,
    namespace: str,
    key: str,
    updated_at: str,
) -> MemoryItem:
    return MemoryItem(
        id=item_id,
        namespace=namespace,
        key=key,
        kind="fact",
        value={"value": key},
        tags=[],
        confidence="high",
        source="operator",
        ttl_seconds=None,
        is_tombstoned=False,
        created_at=updated_at,
        updated_at=updated_at,
    )


class MemoryInjectionTraceTest(unittest.TestCase):
    def test_ordering_is_deterministic(self) -> None:
        item_old = _memory_item(
            "item-old",
            namespace="global",
            key="beta",
            updated_at="2024-01-02T00:00:00+00:00",
        )
        item_new_global = _memory_item(
            "item-new-global",
            namespace="global",
            key="alpha",
            updated_at="2024-01-03T00:00:00+00:00",
        )
        item_new_project = _memory_item(
            "item-new-project",
            namespace="project:alpha",
            key="alpha",
            updated_at="2024-01-03T00:00:00+00:00",
        )
        ordered = order_memory_items([item_old, item_new_project, item_new_global])
        self.assertEqual(
            [item.id for item in ordered],
            ["item-new-global", "item-new-project", "item-old"],
        )

    def test_injection_hash_is_stable(self) -> None:
        item_a = _memory_item(
            "item-a",
            namespace="global",
            key="alpha",
            updated_at="2024-01-04T00:00:00+00:00",
        )
        item_b = _memory_item(
            "item-b",
            namespace="project:alpha",
            key="beta",
            updated_at="2024-01-03T00:00:00+00:00",
        )
        first_hash = injection_hash_for_items([item_a, item_b])
        second_hash = injection_hash_for_items([item_b, item_a])
        self.assertEqual(first_hash, second_hash)


if __name__ == "__main__":
    unittest.main()
