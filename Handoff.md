# Handoff

## Status
- Anchored export outputs to the repo root derived from the database path, with a shared helper and resolved paths.
- Added CLI coverage for export defaults when running from an alternate working directory.
- Documented export defaults in README and operator guide.

## Next Steps
- Consider extending db-anchored path defaults to other user-facing artifacts if new output types are added.

## Tests
- `python scripts/verify.py`
