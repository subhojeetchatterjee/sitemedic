"""
Demo Mode package — recording, curation, and replay of Dynatrace telemetry.

Phase 1: Recorder (this package) wraps the Dynatrace MCP client and logs
         every request/response to disk for later curation and replay.

Phase 2: Curator extracts named scenarios from raw JSONL recordings.
         DemoModeSource serves pre-recorded responses as a drop-in
         replacement for tools/dynatrace_mcp.py.

Controlled by env vars:
  SITEMEDIC_RECORD=true        enables transparent recording (off by default)
  SITEMEDIC_FORCE_DEMO=true    forces demo/replay mode (Phase 5)
  SITEMEDIC_DISABLE_DEMO=true  forces live mode, fails loud if Dynatrace down (Phase 5)
"""
from __future__ import annotations

from demo_mode.recorder import (
    DynatraceRecordingWrapper,
    SessionRecorder,
    get_recorder,
    init_recording,
)
from demo_mode.source import (
    DemoModeSource as _LegacyDemoModeSource,
    get_demo_source,
    load_scenario,
)

# Phase 4+ replay engine — the canonical DemoModeSource
from demo_mode.replay_source import DemoModeSource

__all__ = [
    # Phase 1 — recording
    "SessionRecorder",
    "DynatraceRecordingWrapper",
    "get_recorder",
    "init_recording",
    # Phase 2 — curation & replay (legacy FIFO queue source, kept for compat)
    "get_demo_source",
    "load_scenario",
    # Phase 4 — replay engine (preferred)
    "DemoModeSource",
]
