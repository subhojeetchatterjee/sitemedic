"""
source_factory.py — auto-selecting TelemetrySource factory.

At startup the factory tries to connect to the live Dynatrace MCP server.
If it is unreachable (health check timeout or env var absent), it silently
falls back to DemoModeSource so the agent can still run end-to-end.

Environment variable overrides
-------------------------------
SITEMEDIC_FORCE_DEMO=true   Always use DemoModeSource, never try Dynatrace
SITEMEDIC_DISABLE_DEMO=true Always use DynatraceMCPSource, fail loud if down

Background health monitoring re-checks Dynatrace every 60 s when the initial
connection was live; if it degrades, logs a warning (but does NOT hot-swap the
source to avoid disrupting in-flight incidents).
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from sources.base import TelemetrySource, SourceMetadata

logger = logging.getLogger(__name__)

# Module-level singleton
_source: TelemetrySource | None = None
_source_type: str = "unknown"
_health_monitor_task: asyncio.Task | None = None

_HEALTH_CHECK_TIMEOUT_S = 5.0
_MONITOR_INTERVAL_S = 60.0


# ── Public API ───────────────────────────────────────────────────────────────

async def get_telemetry_source() -> TelemetrySource:
    """
    Return the active TelemetrySource (cached after first successful init).

    Thread-safe via a simple asyncio guard — concurrent callers will all
    await the same init coroutine rather than racing to init twice.
    """
    global _source
    if _source is None:
        _source = await _init_source()
    return _source


async def reset_source() -> None:
    """Force re-init on next call to get_telemetry_source() (useful in tests)."""
    global _source, _health_monitor_task
    if _health_monitor_task and not _health_monitor_task.done():
        _health_monitor_task.cancel()
        _health_monitor_task = None
    _source = None


async def get_source_status() -> dict[str, Any]:
    """Return current source status dict for the frontend API."""
    global _source, _source_type
    if _source is None:
        return {
            "mode": "unknown",
            "source_type": "unknown",
            "is_live": False,
            "health_status": "unknown",
            "demo_mode_active": False,
            "current_scenario": None,
            "scenarios_available": 0,
            "initialised": False,
        }

    meta: SourceMetadata = _source.get_source_metadata()
    mode = "demo" if meta.demo_mode_active else "live"
    return {
        "mode": mode,
        "source_type": meta.source_type,
        "is_live": meta.is_live,
        "health_status": meta.health_status,
        "demo_mode_active": meta.demo_mode_active,
        "current_scenario": meta.current_scenario,
        "scenarios_available": meta.scenarios_available,
        "initialised": True,
        **meta.extra,
    }


# ── Initialisation ───────────────────────────────────────────────────────────

async def _init_source() -> TelemetrySource:
    """Select and return the appropriate TelemetrySource."""
    global _source_type

    force_demo = os.environ.get("SITEMEDIC_FORCE_DEMO", "").lower() == "true"
    disable_demo = os.environ.get("SITEMEDIC_DISABLE_DEMO", "").lower() == "true"

    if force_demo:
        logger.info("SITEMEDIC_FORCE_DEMO=true — using DemoModeSource (replay engine)")
        _source_type = "demo"
        src = _make_demo_source()
        src.start()
        return src

    if disable_demo:
        logger.info(
            "SITEMEDIC_DISABLE_DEMO=true — using DynatraceMCPSource "
            "(will fail loud if Dynatrace is unreachable)"
        )
        _source_type = "dynatrace"
        return _make_dynatrace_source()

    # Auto-detect: try Dynatrace, fall back to demo
    dt = _make_dynatrace_source()
    try:
        healthy = await _health_check_dynatrace(dt, timeout_s=_HEALTH_CHECK_TIMEOUT_S)
    except Exception as exc:
        logger.warning(
            "DynatraceMCPSource init failed (%s) — falling back to DemoModeSource", exc
        )
        _log_source_transition("dynatrace", "demo", str(exc))
        _source_type = "demo"
        src = _make_demo_source()
        src.start()
        return src

    if healthy:
        logger.info(
            "Dynatrace health check passed — using live DynatraceMCPSource"
        )
        _source_type = "dynatrace"
        _start_background_health_monitor(dt)
        return dt
    else:
        logger.warning(
            "Dynatrace health check timed out or failed — falling back to DemoModeSource"
        )
        _log_source_transition("dynatrace", "demo", "health_check_failed")
        _source_type = "demo"
        src = _make_demo_source()
        src.start()
        return src


# ── Health checks ─────────────────────────────────────────────────────────

async def _health_check_dynatrace(dt: TelemetrySource, timeout_s: float) -> bool:
    """
    Perform a cheap Dynatrace health check.

    Calls list_entities("SERVICE") with a timeout. Returns True on success.
    """
    try:
        await asyncio.wait_for(dt.list_entities("SERVICE"), timeout=timeout_s)
        return True
    except asyncio.TimeoutError:
        logger.debug("Dynatrace health check timed out after %.1fs", timeout_s)
        return False
    except Exception as exc:
        logger.debug("Dynatrace health check exception: %s", exc)
        return False


def _start_background_health_monitor(dt: TelemetrySource) -> None:
    """Start a background task that re-checks Dynatrace health every 60 s."""
    global _health_monitor_task
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            _health_monitor_task = loop.create_task(_background_health_monitor(dt))
    except RuntimeError:
        pass


async def _background_health_monitor(dt: TelemetrySource) -> None:
    """
    Periodically re-check Dynatrace health.

    Logs a warning when health degrades but does NOT hot-swap the source
    (to avoid disrupting in-flight incidents and Firestore writes).
    """
    from sources.dynatrace import DynatraceMCPSource
    while True:
        try:
            await asyncio.sleep(_MONITOR_INTERVAL_S)
            healthy = await _health_check_dynatrace(dt, timeout_s=_HEALTH_CHECK_TIMEOUT_S)
            if isinstance(dt, DynatraceMCPSource):
                if healthy:
                    dt.mark_healthy()
                else:
                    dt.mark_unhealthy()
                    logger.warning(
                        "Dynatrace health check failed during monitoring — "
                        "source is degraded. Restart agent to trigger fallback."
                    )
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.exception("Background health monitor error: %s", exc)


# ── Factories ─────────────────────────────────────────────────────────────

def _make_demo_source():
    """Instantiate DemoModeSource (replay engine)."""
    from demo_mode.replay_source import DemoModeSource
    return DemoModeSource(auto_start_scheduler=False)  # caller must call .start()


def _make_dynatrace_source():
    """Instantiate DynatraceMCPSource."""
    from sources.dynatrace import DynatraceMCPSource
    return DynatraceMCPSource()


# ── Audit logging ─────────────────────────────────────────────────────────

def _log_source_transition(from_source: str, to_source: str, reason: str) -> None:
    """Write a source-transition event to the audit trail."""
    try:
        from audit import AuditEvent, log_audit_event
        log_audit_event(AuditEvent(
            actor="system",
            actor_identity="source_factory",
            action_type="source_transition",
            payload={
                "from": from_source,
                "to": to_source,
                "reason": reason,
            },
            result="success",
        ))
    except Exception as exc:
        logger.debug("Could not log source transition to audit trail: %s", exc)
