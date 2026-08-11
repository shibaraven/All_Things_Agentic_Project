"""Asynchronous Firestore and Pub/Sub evidence bridge.

Cloud calls run on a bounded background worker so the deterministic control
loop never waits for an external service. The in-memory event/read model stays
available as the fail-safe path, while Firestore and Pub/Sub provide durable
competition evidence and replay data when configured.
"""

from __future__ import annotations

import json
import os
import queue
import threading
import time
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class EvidenceEnvelope:
    event: dict[str, Any]
    snapshot: Optional[dict[str, Any]] = None


class EvidenceBridge:
    """Queue-backed optional Google Cloud evidence sink."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        project_id: str = "",
        topic_id: str = "shiftzero-events",
        firestore_database: str = "(default)",
        max_queue: int = 5_000,
    ) -> None:
        self.enabled = enabled and bool(project_id)
        self.project_id = project_id
        self.topic_id = topic_id
        self.firestore_database = firestore_database
        self._queue: queue.Queue[EvidenceEnvelope | None] = queue.Queue(maxsize=max_queue)
        self._worker: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._connected = False
        self._published = 0
        self._firestore_writes = 0
        self._dropped = 0
        self._errors = 0
        self._last_error: Optional[str] = None
        if self.enabled:
            self._worker = threading.Thread(
                target=self._run,
                name="shiftzero-cloud-evidence",
                daemon=True,
            )
            self._worker.start()

    @classmethod
    def from_env(cls) -> "EvidenceBridge":
        enabled = os.getenv("SHIFTZERO_CLOUD_EVIDENCE_ENABLED", "false").lower() in {
            "1",
            "true",
            "yes",
        }
        return cls(
            enabled=enabled,
            project_id=os.getenv("GOOGLE_CLOUD_PROJECT", ""),
            topic_id=os.getenv("SHIFTZERO_PUBSUB_TOPIC", "shiftzero-events"),
            firestore_database=os.getenv("SHIFTZERO_FIRESTORE_DATABASE", "(default)"),
        )

    def record(
        self,
        event: dict[str, Any],
        snapshot: Optional[dict[str, Any]] = None,
    ) -> None:
        if not self.enabled:
            return
        envelope = EvidenceEnvelope(deepcopy(event), deepcopy(snapshot) if snapshot else None)
        try:
            self._queue.put_nowait(envelope)
        except queue.Full:
            with self._lock:
                self._dropped += 1
                self._last_error = "evidence queue full"

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "provider": "google-cloud" if self.enabled else "in-memory",
                "configured": self.enabled,
                "connected": self._connected,
                "project_id": self.project_id or None,
                "firestore_database": self.firestore_database if self.enabled else None,
                "pubsub_topic": self.topic_id if self.enabled else None,
                "queued": self._queue.qsize() if self.enabled else 0,
                "events_published": self._published,
                "firestore_writes": self._firestore_writes,
                "dropped": self._dropped,
                "errors": self._errors,
                "last_error": self._last_error,
            }

    def flush(self, timeout: float = 10.0) -> bool:
        if not self.enabled:
            return True
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.02)
        return self._queue.unfinished_tasks == 0

    def close(self) -> None:
        if not self.enabled or self._worker is None:
            return
        self.flush()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            return
        self._worker.join(timeout=3.0)

    def _run(self) -> None:
        try:
            from google.cloud import firestore
            from google.cloud import pubsub_v1

            firestore_client = firestore.Client(
                project=self.project_id,
                database=self.firestore_database,
            )
            publisher = pubsub_v1.PublisherClient()
            topic_path = publisher.topic_path(self.project_id, self.topic_id)
            with self._lock:
                self._connected = True
        except Exception as exc:
            self._record_error(exc)
            self._drain_without_delivery()
            return

        while True:
            envelope = self._queue.get()
            if envelope is None:
                self._queue.task_done()
                return
            try:
                event = envelope.event
                event_id = f"{event.get('shift_id') or 'none'}-{int(event.get('id', 0)):08d}"
                firestore_client.collection("shiftzero_events").document(event_id).set(event)
                with self._lock:
                    self._firestore_writes += 1
                if envelope.snapshot and event.get("shift_id"):
                    firestore_client.collection("shiftzero_shifts").document(
                        str(event["shift_id"])
                    ).set(envelope.snapshot)
                    with self._lock:
                        self._firestore_writes += 1
                data = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                publisher.publish(
                    topic_path,
                    data,
                    event_type=str(event.get("event_type", "unknown")),
                    shift_id=str(event.get("shift_id") or "none"),
                    trace_id=str(event.get("trace_id") or "none"),
                ).result(timeout=10.0)
                with self._lock:
                    self._published += 1
            except Exception as exc:
                self._record_error(exc)
            finally:
                self._queue.task_done()

    def _record_error(self, exc: Exception) -> None:
        with self._lock:
            self._errors += 1
            self._last_error = f"{type(exc).__name__}: {exc}"[:500]

    def _drain_without_delivery(self) -> None:
        while True:
            envelope = self._queue.get()
            self._queue.task_done()
            if envelope is None:
                return
            with self._lock:
                self._dropped += 1
