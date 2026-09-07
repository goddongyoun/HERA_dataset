"""Structured, append-only event logging for HERA v2 experiments.

Every record carries the complete experiment and request identity.  Wall-clock
UTC is useful for cross-process correlation; ``monotonic_ns`` is the clock to
use for durations within a process.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Protocol


class EventStage(str, Enum):
    TRIAL_STARTED = "trial_started"
    REQUEST_QUEUED = "request_queued"
    REQUEST_DISPATCHED = "request_dispatched"
    CONNECTION_OPENING = "connection_opening"
    RESPONSE_HEADERS = "response_headers"
    SERVER_OBSERVED = "server_observed"
    FIRST_CHUNK = "first_chunk"
    RESPONSE_COMPLETE = "response_complete"
    CANCEL_REQUESTED = "cancel_requested"
    CONNECTION_CLOSED = "connection_closed"
    REQUEST_CANCELLED = "request_cancelled"
    REQUEST_FAILED = "request_failed"
    RESPONSE_REJECTED_STALE = "response_rejected_stale"
    RESPONSE_REJECTED_INVALID = "response_rejected_invalid"
    RESPONSE_ACCEPTED = "response_accepted"
    MISSION_BARRIER_PASSED = "mission_barrier_passed"
    FAULT_TRIGGERED = "fault_triggered"
    RETRY_SCHEDULED = "retry_scheduled"
    TRIAL_COMPLETED = "trial_completed"


@dataclass(frozen=True, slots=True)
class EventIdentity:
    """Identity fields required on every event, including trial-level events."""

    batch_id: str
    run_id: str
    trial_id: str
    request_id: str | None
    request_type: str
    generation: int


@dataclass(frozen=True, slots=True)
class EventRecord:
    event_version: int
    event_id: str
    sequence: int
    batch_id: str
    run_id: str
    trial_id: str
    request_id: str | None
    request_type: str
    generation: int
    stage: str
    monotonic_ns: int
    utc: str
    details: Mapping[str, Any]


class EventLogger(Protocol):
    def emit(
        self,
        stage: EventStage | str,
        identity: EventIdentity,
        details: Mapping[str, Any] | None = None,
    ) -> EventRecord:
        """Append or retain one immutable event and return its record."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _json_default(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


class JsonlEventLogger:
    """Process-local, thread-safe JSONL writer that only opens files in append mode.

    A record is encoded into one line and handed to a single ``os.write`` call
    while holding a lock.  This prevents interleaving between threads sharing
    this logger.  Separate processes should write separate run logs and merge by
    event identity; cross-process file locking is deliberately not implied.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._sequence = 0

    def emit(
        self,
        stage: EventStage | str,
        identity: EventIdentity,
        details: Mapping[str, Any] | None = None,
    ) -> EventRecord:
        stage_value = stage.value if isinstance(stage, EventStage) else str(stage)
        with self._lock:
            self._sequence += 1
            record = EventRecord(
                event_version=1,
                event_id=str(uuid.uuid4()),
                sequence=self._sequence,
                batch_id=identity.batch_id,
                run_id=identity.run_id,
                trial_id=identity.trial_id,
                request_id=identity.request_id,
                request_type=identity.request_type,
                generation=identity.generation,
                stage=stage_value,
                monotonic_ns=time.perf_counter_ns(),
                utc=_utc_now(),
                details=dict(details or {}),
            )
            payload = (
                json.dumps(
                    asdict(record),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=_json_default,
                    allow_nan=False,
                )
                + "\n"
            ).encode("utf-8")
            descriptor = os.open(
                self.path,
                os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_BINARY", 0),
                0o644,
            )
            try:
                os.write(descriptor, payload)
            finally:
                os.close(descriptor)
        return record


class MemoryEventLogger:
    """In-memory logger for deterministic tests and embedding applications."""

    def __init__(self) -> None:
        self.records: list[EventRecord] = []
        self._lock = threading.Lock()

    def emit(
        self,
        stage: EventStage | str,
        identity: EventIdentity,
        details: Mapping[str, Any] | None = None,
    ) -> EventRecord:
        stage_value = stage.value if isinstance(stage, EventStage) else str(stage)
        with self._lock:
            record = EventRecord(
                event_version=1,
                event_id=str(uuid.uuid4()),
                sequence=len(self.records) + 1,
                batch_id=identity.batch_id,
                run_id=identity.run_id,
                trial_id=identity.trial_id,
                request_id=identity.request_id,
                request_type=identity.request_type,
                generation=identity.generation,
                stage=stage_value,
                monotonic_ns=time.perf_counter_ns(),
                utc=_utc_now(),
                details=dict(details or {}),
            )
            self.records.append(record)
            return record
