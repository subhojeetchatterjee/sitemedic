"""
Transparent recording wrapper for the Dynatrace MCP client and Gemini calls.

Enabled via SITEMEDIC_RECORD=true. Off by default — recording is always opt-in.
Output: agent/demo_mode/recordings/raw/<session_id>/<file>.jsonl

Each JSONL file has one JSON object per line:
  { "type": "mcp_call"|"gemini_call"|"firestore_write"|"incident_event",
    "ts": <iso8601>,
    "latency_ms": <int>,
    ... type-specific fields }

Files rotate at 10 MB. Partial sessions are recoverable (append-only).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

_RECORDINGS_DIR = Path(__file__).parent / "recordings" / "raw"
_MAX_FILE_BYTES = 10 * 1024 * 1024  # 10 MB


class SessionRecorder:
    """
    Singleton-ish per-session recorder. Create one at agent startup when
    SITEMEDIC_RECORD=true; pass it into DynatraceRecordingWrapper and the
    Gemini call helper.
    """

    def __init__(self, session_id: str | None = None):
        self.session_id = session_id or f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}"
        self._dir = _RECORDINGS_DIR / self.session_id
        self._dir.mkdir(parents=True, exist_ok=True)
        self._file_index = 0
        self._current_file: Path = self._next_file()
        self._bytes_written = 0
        logger.info("SessionRecorder started: %s", self._dir)

    def _next_file(self) -> Path:
        self._file_index += 1
        return self._dir / f"events_{self._file_index:04d}.jsonl"

    def _rotate_if_needed(self) -> None:
        if self._bytes_written >= _MAX_FILE_BYTES:
            self._current_file = self._next_file()
            self._bytes_written = 0
            logger.debug("Recorder: rotated to %s", self._current_file)

    def record(self, event: dict) -> None:
        """Append one event dict to the current JSONL file."""
        self._rotate_if_needed()
        line = json.dumps(event, default=str) + "\n"
        encoded = line.encode("utf-8")
        with self._current_file.open("ab") as f:
            f.write(encoded)
        self._bytes_written += len(encoded)

    def record_mcp_call(
        self,
        tool: str,
        arguments: dict,
        response: Any,
        latency_ms: int,
        error: str | None = None,
    ) -> None:
        self.record({
            "type": "mcp_call",
            "ts": datetime.now(timezone.utc).isoformat(),
            "latency_ms": latency_ms,
            "tool": tool,
            "arguments": arguments,
            "args_hash": _hash_args(tool, arguments),
            "response": response,
            "error": error,
        })

    def record_gemini_call(
        self,
        model: str,
        prompt_summary: str,
        response_text: str,
        input_tokens: int,
        output_tokens: int,
        latency_ms: int,
        error: str | None = None,
    ) -> None:
        self.record({
            "type": "gemini_call",
            "ts": datetime.now(timezone.utc).isoformat(),
            "latency_ms": latency_ms,
            "model": model,
            "prompt_summary": prompt_summary,
            "response_text": response_text,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "error": error,
        })

    def record_firestore_write(
        self,
        collection: str,
        doc_id: str,
        operation: str,  # "set" | "update" | "array_union"
        payload: dict,
    ) -> None:
        self.record({
            "type": "firestore_write",
            "ts": datetime.now(timezone.utc).isoformat(),
            "collection": collection,
            "doc_id": doc_id,
            "operation": operation,
            "payload": payload,
        })

    def record_incident_event(
        self,
        incident_id: str,
        event_type: str,  # "created"|"status_change"|"plan_set"|"approved"|"remediated"|"postmortem"
        data: dict,
    ) -> None:
        self.record({
            "type": "incident_event",
            "ts": datetime.now(timezone.utc).isoformat(),
            "incident_id": incident_id,
            "event_type": event_type,
            "data": data,
        })

    def get_session_dir(self) -> Path:
        return self._dir


def _hash_args(tool: str, arguments: dict) -> str:
    """Stable hash of (tool_name, argument_values) for response lookup in replay."""
    canonical = json.dumps({"tool": tool, "args": arguments}, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


# ── Module-level singleton ─────────────────────────────────────────────────

_recorder: SessionRecorder | None = None


def get_recorder() -> SessionRecorder | None:
    """Returns the active recorder if recording is enabled, else None."""
    return _recorder


def init_recording() -> SessionRecorder | None:
    """
    Call once at agent startup. Returns a SessionRecorder if
    SITEMEDIC_RECORD=true, else returns None and does nothing.
    """
    global _recorder
    if os.environ.get("SITEMEDIC_RECORD", "").lower() != "true":
        return None
    if _recorder is not None:
        return _recorder
    _recorder = SessionRecorder()
    logger.info("Recording enabled. Session: %s", _recorder.session_id)
    return _recorder


# ── Recording wrapper for Dynatrace MCP ───────────────────────────────────

class DynatraceRecordingWrapper:
    """
    Wraps every function in dynatrace_mcp and records call + response.
    Drop-in replacement: same call signatures, same return values.
    Only active when a recorder is provided.
    """

    def __init__(self, recorder: SessionRecorder | None = None):
        from tools import dynatrace_mcp as _dt
        self._dt = _dt
        self._rec = recorder

    def _wrap(self, tool: str, fn: Callable, arguments: dict) -> Any:
        import asyncio

        async def _call():
            t0 = time.monotonic()
            error = None
            result = None
            try:
                result = await fn(**arguments)
                return result
            except Exception as exc:
                error = str(exc)
                raise
            finally:
                latency_ms = int((time.monotonic() - t0) * 1000)
                if self._rec:
                    self._rec.record_mcp_call(
                        tool=tool,
                        arguments=arguments,
                        response=result,
                        latency_ms=latency_ms,
                        error=error,
                    )
        return _call()

    async def list_problems(self, status: str = "OPEN") -> list[dict]:
        return await self._wrap("list_problems", self._dt.list_problems, {"status": status})

    async def get_problem_details(self, problem_id: str) -> dict:
        return await self._wrap("get_problem_details", self._dt.get_problem_details, {"problem_id": problem_id})

    async def query_metrics(
        self,
        metric_selector: str,
        entity_selector: str | None = None,
        from_time: str = "now-1h",
        to_time: str = "now",
        resolution: str = "1m",
    ) -> dict:
        args = {
            "metric_selector": metric_selector,
            "from_time": from_time,
            "to_time": to_time,
            "resolution": resolution,
        }
        if entity_selector is not None:
            args["entity_selector"] = entity_selector
        return await self._wrap("query_metrics", self._dt.query_metrics, args)

    async def get_traces(
        self,
        service_name: str,
        from_time: str = "now-30m",
        to_time: str = "now",
        limit: int = 20,
    ) -> list[dict]:
        return await self._wrap("get_traces", self._dt.get_traces, {
            "service_name": service_name,
            "from_time": from_time,
            "to_time": to_time,
            "limit": limit,
        })

    async def list_entities(self, entity_type: str = "SERVICE") -> list[dict]:
        return await self._wrap("list_entities", self._dt.list_entities, {"entity_type": entity_type})

    async def get_service_response_time(self, service_name: str) -> dict:
        return await self._wrap("get_service_response_time", self._dt.get_service_response_time, {"service_name": service_name})

    async def get_error_rate(self, service_name: str) -> dict:
        return await self._wrap("get_error_rate", self._dt.get_error_rate, {"service_name": service_name})
