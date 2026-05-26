"""
Thin async wrapper around the Dynatrace MCP server.

The MCP server exposes a JSON-RPC 2.0 HTTP interface. Each tool call is a
POST to /mcp with {"method": "tools/call", "params": {"name": ..., "arguments": ...}}.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from typing import Any

import httpx

_MCP_URL: str = ""
_OAUTH_TOKEN: str = ""
_OAUTH_TOKEN_EXPIRES: datetime | None = None


def _url() -> str:
    """Return the MCP endpoint URL, always ending in /mcp exactly once."""
    global _MCP_URL
    if not _MCP_URL:
        base = os.environ.get("DT_MCP_SERVER_URL", "http://localhost:3001").rstrip("/")
        # The env var may already include the /mcp path suffix — normalise so we
        # don't end up posting to /mcp/mcp.
        if not base.endswith("/mcp"):
            base = f"{base}/mcp"
        _MCP_URL = base
    return _MCP_URL


async def _fetch_oauth_token() -> str:
    """Fetch a Bearer token using OAuth2 client credentials flow."""
    global _OAUTH_TOKEN, _OAUTH_TOKEN_EXPIRES

    # Check if cached token is still valid (with 60s safety margin)
    if _OAUTH_TOKEN and _OAUTH_TOKEN_EXPIRES:
        if datetime.now() < _OAUTH_TOKEN_EXPIRES - timedelta(seconds=60):
            return _OAUTH_TOKEN

    client_id = os.environ.get("DT_OAUTH_CLIENT_ID", "")
    client_secret = os.environ.get("DT_OAUTH_CLIENT_SECRET", "")
    tenant_url = os.environ.get("DT_TENANT_URL", "https://dpl22780.live.dynatrace.com").rstrip("/")

    if not client_id or not client_secret:
        raise ValueError("DT_OAUTH_CLIENT_ID and DT_OAUTH_CLIENT_SECRET required for OAuth")

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            "https://sso.dynatrace.com/sso/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        data = resp.json()
        _OAUTH_TOKEN = data["access_token"]
        expires_in_seconds = data.get("expires_in", 3600)
        _OAUTH_TOKEN_EXPIRES = datetime.now() + timedelta(seconds=expires_in_seconds)
        return _OAUTH_TOKEN


async def _auth_headers() -> dict:
    """Build the Authorization header using OAuth Bearer token."""
    try:
        token = await _fetch_oauth_token()
        return {"Authorization": f"Bearer {token}"}
    except Exception:
        # Fallback to API token if OAuth fails (for backwards compatibility)
        api_token = os.environ.get("DT_API_TOKEN", "")
        if api_token:
            return {"Authorization": f"Bearer {api_token}"}
        return {}


def _parse_dt_text_response(text: str) -> Any:
    """
    Parse the Dynatrace MCP text response format:
      'Query metadata:\n{JSON}\nResults:\n{JSON or table}'
    Returns parsed results list, or the metadata dict, or the raw text.
    """
    import re
    # Try to find a Results section after the metadata JSON block
    results_match = re.search(r'Results:\s*(\[.*?\]|\{.*?\})', text, re.DOTALL)
    if results_match:
        try:
            return json.loads(results_match.group(1))
        except json.JSONDecodeError:
            pass

    # Try to extract the metadata JSON block (between first { and matching })
    meta_match = re.search(r'Query metadata:\s*(\{.*\})\s*$', text, re.DOTALL)
    if meta_match:
        try:
            meta = json.loads(meta_match.group(1))
            # If scannedRecords is 0, return empty list (no results)
            if meta.get("grail", {}).get("scannedRecords", 0) == 0:
                return []
            return meta
        except json.JSONDecodeError:
            pass

    return text  # last resort


async def _call(tool: str, arguments: dict[str, Any] | None = None) -> Any:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments or {}},
    }
    t0 = time.monotonic()
    error: str | None = None
    result: Any = None
    try:
        headers = await _auth_headers()
        async with httpx.AsyncClient(timeout=30, headers=headers) as client:
            resp = await client.post(_url(), json=payload)
            resp.raise_for_status()
            body = resp.json()
            if "error" in body:
                raise RuntimeError(f"MCP error from {tool}: {body['error']}")
            # MCP returns content as list of {type, text} blocks
            content = body.get("result", {}).get("content", [])
            if content and content[0].get("type") == "text":
                raw_text = content[0]["text"]
                try:
                    result = json.loads(raw_text)
                except json.JSONDecodeError:
                    # Dynatrace MCP returns a human-readable text format for DQL results
                    result = _parse_dt_text_response(raw_text)
            else:
                result = body.get("result")
            return result
    except Exception as exc:
        error = str(exc)
        raise
    finally:
        latency_ms = int((time.monotonic() - t0) * 1000)
        try:
            from demo_mode.recorder import get_recorder
            rec = get_recorder()
            if rec is not None:
                rec.record_mcp_call(
                    tool=tool,
                    arguments=arguments or {},
                    response=result,
                    latency_ms=latency_ms,
                    error=error,
                )
        except Exception:
            pass  # never let recording break the live path


async def list_problems(status: str = "OPEN") -> list[dict]:
    """List current Dynatrace problems (ACTIVE, CLOSED, or ALL)."""
    dt_status = "ACTIVE" if status == "OPEN" else status
    raw = await _call("query-problems", {"status": dt_status, "history": "2h"})

    # Normalize into a list of dicts with keys the orchestrator expects
    if isinstance(raw, list):
        normalized = []
        for item in raw:
            if isinstance(item, dict):
                normalized.append({
                    "problemId": item.get("event.id") or item.get("problemId") or item.get("id", ""),
                    "title": item.get("event.description") or item.get("title", "Unknown"),
                    "status": item.get("event.status") or item.get("status", dt_status),
                    "severityLevel": item.get("event.category") or item.get("severityLevel", "UNKNOWN"),
                    "startTime": item.get("event.start") or item.get("startTime"),
                    "impactedEntities": [
                        {"name": eid} for eid in (item.get("affected_entity_ids") or [])
                    ],
                    "_raw": item,
                })
        return normalized
    # Already empty list (scannedRecords=0) or unexpected type
    return [] if not isinstance(raw, list) else raw


async def get_problem_details(problem_id: str) -> dict:
    """Full details + root cause analysis for a specific problem."""
    return await _call("get-problem-by-id", {"problemId": problem_id})


async def query_metrics(
    metric_selector: str,
    entity_selector: str | None = None,
    from_time: str = "now-1h",
    to_time: str = "now",
    resolution: str = "1m",
) -> dict:
    """Run a Dynatrace metrics query via DQL."""
    # Build a DQL query from metric_selector and entity_selector
    service_filter = ""
    if entity_selector:
        # Extract service name from entityName("...") pattern
        import re
        match = re.search(r'entityName\("([^"]+)"\)', entity_selector)
        if match:
            service_filter = f', filter: {{eq("dt.entity.service.name", "{match.group(1)}")}}'

    # Strip surrounding quotes if Gemini passes "now-1h" style strings
    from_dql = from_time.strip('"') if from_time.startswith('"') else from_time
    to_dql = to_time.strip('"') if to_time.startswith('"') else to_time
    dql = (
        f'timeseries avg({metric_selector}){service_filter}, '
        f'from: {from_dql}, to: {to_dql}, interval: "{resolution}"'
    )
    return await _call("execute-dql", {"dqlQueryString": dql})


async def get_traces(
    service_name: str,
    from_time: str = "now-30m",
    to_time: str = "now",
    limit: int = 20,
) -> list[dict]:
    """Fetch distributed traces for a service via DQL."""
    from_dql = from_time.strip('"') if from_time.startswith('"') else from_time
    to_dql = to_time.strip('"') if to_time.startswith('"') else to_time
    dql = (
        f'fetch spans, from: {from_dql}, to: {to_dql} '
        f'| filter service.name == "{service_name}" '
        f'| sort duration desc '
        f'| limit {limit}'
    )
    return await _call("execute-dql", {"dqlQueryString": dql})


async def list_entities(entity_type: str = "SERVICE", name_filter: str = "") -> list[dict]:
    """List monitored entities (services, hosts, processes)."""
    dt_type = f"dt.entity.{entity_type.lower()}"
    return await _call("get-entity-id", {"entityType": dt_type, "entityNameFilter": name_filter or ""})


async def get_service_response_time(service_name: str) -> dict:
    """Convenience: p50/p90/p99 response time for a service over the last hour."""
    dql = (
        f'timeseries p50=percentile(dt.service.request.response_time, 50), '
        f'p90=percentile(dt.service.request.response_time, 90), '
        f'p99=percentile(dt.service.request.response_time, 99), '
        f'from: now()-1h, interval: "1m" '
        f'| filter service.name == "{service_name}"'
    )
    return await _call("execute-dql", {"dqlQueryString": dql})


async def get_error_rate(service_name: str) -> dict:
    """Convenience: error rate for a service over the last hour."""
    dql = (
        f'timeseries error_rate=avg(dt.service.request.failure_rate), '
        f'from: now()-1h, interval: "1m" '
        f'| filter service.name == "{service_name}"'
    )
    return await _call("execute-dql", {"dqlQueryString": dql})
