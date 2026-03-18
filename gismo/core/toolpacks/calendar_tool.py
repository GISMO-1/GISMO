"""Calendar execution tool for local GISMO events."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from gismo.core.models import CalendarEvent
from gismo.core.state import StateStore
from gismo.core.tools import Tool


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("datetime value is required")
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    else:
        raise ValueError("datetime value is required")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo or timezone.utc)
    return dt.astimezone(timezone.utc)


def _dt_text(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _serialize_event(event: CalendarEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "title": event.title,
        "description": event.description,
        "event_type": event.event_type,
        "status": event.status,
        "start_at": _dt_text(event.start_at),
        "end_at": _dt_text(event.end_at),
        "all_day": bool(event.all_day),
        "source": event.source,
        "source_ref": event.source_ref,
        "requires_ack": bool(event.requires_ack),
        "metadata_json": event.metadata_json,
        "created_at": _dt_text(event.created_at),
        "updated_at": _dt_text(event.updated_at),
    }


def _event_from_payload(payload: dict[str, Any], *, existing: CalendarEvent | None = None) -> CalendarEvent:
    title = str(payload.get("title") or (existing.title if existing else "")).strip()
    if not title:
        raise ValueError("title is required")
    start_at = _coerce_dt(payload.get("start_at", existing.start_at if existing else None))
    end_value = payload.get("end_at", existing.end_at if existing else None)
    end_at = _coerce_dt(end_value) if end_value else None
    if end_at is None:
        end_at = start_at + timedelta(hours=1)
    if end_at < start_at:
        raise ValueError("end_at must be after start_at")
    metadata_json = payload.get("metadata_json", existing.metadata_json if existing else {}) or {}
    if not isinstance(metadata_json, dict):
        raise ValueError("metadata_json must be an object")
    return CalendarEvent(
        id=existing.id if existing else str(payload.get("id") or CalendarEvent(title=title, start_at=start_at).id),
        title=title,
        description=str(payload.get("description", existing.description if existing else "") or "").strip(),
        event_type=str(payload.get("event_type", existing.event_type if existing else "event") or "event").strip() or "event",
        status=str(payload.get("status", existing.status if existing else "scheduled") or "scheduled").strip() or "scheduled",
        start_at=start_at,
        end_at=end_at,
        all_day=bool(payload.get("all_day", existing.all_day if existing else False)),
        source=str(payload.get("source", existing.source if existing else "system") or "system").strip() or "system",
        source_ref=str(payload.get("source_ref", existing.source_ref if existing else "") or "").strip(),
        requires_ack=bool(payload.get("requires_ack", existing.requires_ack if existing else False)),
        metadata_json=metadata_json,
        created_at=existing.created_at if existing else _utc_now(),
        updated_at=_utc_now(),
    )


def _format_when(value: datetime, *, all_day: bool) -> str:
    local = value.astimezone(datetime.now().astimezone().tzinfo or timezone.utc)
    if all_day:
        return local.strftime("%A, %B %d")
    if local.strftime("%p"):
        try:
            return local.strftime("%A at %#I:%M %p")
        except ValueError:
            return local.strftime("%A at %-I:%M %p")
    return local.isoformat()


class CalendarControlTool(Tool):
    def __init__(self, state_store: StateStore) -> None:
        super().__init__(
            name="calendar_control",
            description="Create, update, remove, and query local calendar events",
            schema={"type": "object"},
        )
        self._state_store = state_store

    def run(self, tool_input: dict[str, Any]) -> dict[str, Any]:
        action = str(tool_input.get("action") or "").strip().lower()
        payload = tool_input.get("payload") or {}
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")

        if action == "add":
            return self._add(payload)
        if action == "update":
            return self._update(payload)
        if action == "delete":
            return self._delete(payload)
        if action == "delete_range":
            return self._delete_range(payload)
        if action == "list":
            return self._list(payload)
        raise ValueError(f"Unsupported calendar action '{action}'")

    def _add(self, payload: dict[str, Any]) -> dict[str, Any]:
        event = _event_from_payload(payload)
        stored = self._state_store.upsert_calendar_event(event)
        summary = f"I added {stored.title} to your calendar for {_format_when(stored.start_at, all_day=stored.all_day)}."
        self._state_store.record_event(
            actor="worker",
            event_type="calendar_created",
            message=summary,
            json_payload={"calendar_event_id": stored.id},
        )
        return {"summary": summary, "event": _serialize_event(stored)}

    def _update(self, payload: dict[str, Any]) -> dict[str, Any]:
        event_id = str(payload.get("id") or "").strip()
        if not event_id:
            raise ValueError("id is required")
        existing = self._state_store.get_calendar_event(event_id)
        if existing is None:
            raise ValueError(f"Calendar event not found: {event_id}")
        stored = self._state_store.upsert_calendar_event(_event_from_payload(payload, existing=existing))
        summary = f"I updated {stored.title} on your calendar."
        self._state_store.record_event(
            actor="worker",
            event_type="calendar_updated",
            message=summary,
            json_payload={"calendar_event_id": stored.id},
        )
        return {"summary": summary, "event": _serialize_event(stored)}

    def _delete(self, payload: dict[str, Any]) -> dict[str, Any]:
        event_id = str(payload.get("id") or "").strip()
        if not event_id:
            raise ValueError("id is required")
        existing = self._state_store.get_calendar_event(event_id)
        if existing is None:
            raise ValueError(f"Calendar event not found: {event_id}")
        self._state_store.delete_calendar_event(event_id)
        summary = f"I removed {existing.title} from your calendar."
        self._state_store.record_event(
            actor="worker",
            event_type="calendar_deleted",
            message=summary,
            json_payload={"calendar_event_id": event_id},
        )
        return {"summary": summary, "deleted_event_id": event_id}

    def _delete_range(self, payload: dict[str, Any]) -> dict[str, Any]:
        start_at = _coerce_dt(payload.get("start_at"))
        end_at = _coerce_dt(payload.get("end_at"))
        match_text = str(payload.get("match_text") or "").strip().lower()
        events = self._state_store.list_calendar_events(start_at=start_at, end_at=end_at, limit=500)
        if match_text:
            events = [
                event
                for event in events
                if match_text in " ".join(
                    [
                        event.title.lower(),
                        event.description.lower(),
                        event.source_ref.lower(),
                    ]
                )
            ]
        deleted_titles: list[str] = []
        for event in events:
            if self._state_store.delete_calendar_event(event.id):
                deleted_titles.append(event.title)
        if not deleted_titles:
            summary = "I did not find any calendar events to remove in that time range."
        else:
            summary = f"I removed {len(deleted_titles)} calendar event{'s' if len(deleted_titles) != 1 else ''}."
        self._state_store.record_event(
            actor="worker",
            event_type="calendar_deleted_range",
            message=summary,
            json_payload={
                "start_at": _dt_text(start_at),
                "end_at": _dt_text(end_at),
                "match_text": match_text,
                "deleted_titles": deleted_titles,
            },
        )
        return {
            "summary": summary,
            "deleted_count": len(deleted_titles),
            "deleted_titles": deleted_titles,
        }

    def _list(self, payload: dict[str, Any]) -> dict[str, Any]:
        start_at = _coerce_dt(payload.get("start_at")) if payload.get("start_at") else None
        end_at = _coerce_dt(payload.get("end_at")) if payload.get("end_at") else None
        events = self._state_store.list_calendar_events(start_at=start_at, end_at=end_at, limit=200)
        summary = (
            "Your calendar is clear."
            if not events
            else f"I found {len(events)} calendar event{'s' if len(events) != 1 else ''}."
        )
        return {"summary": summary, "events": [_serialize_event(event) for event in events]}
