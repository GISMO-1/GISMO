# Decisions

- **Python stdlib-first** implementation with SQLite persistence for auditability and portability.
- **Deny-by-default permissions** for all tools and actuators.
- **Audit trail as first-class** data captured for every significant action.
- **Retries at the tool-call level** with explicit status tracking.
- **Idempotency** enforced via `(idempotency_key, input_hash)`.
- **Task graph scheduling** respects dependencies, failure propagation, and deadlock detection.
