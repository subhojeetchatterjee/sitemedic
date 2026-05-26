"""
TelemetrySource abstraction layer.

Provides a unified interface over Dynatrace MCP (live) and DemoModeSource
(recorded playback), enabling transparent source-swapping at runtime.
"""
from sources.base import TelemetrySource, SourceMetadata

__all__ = ["TelemetrySource", "SourceMetadata"]
