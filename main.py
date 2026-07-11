"""Ollama Cloud Usage Monitor — Proxy Mode.

Acts as a transparent proxy for Ollama API calls. After each completed
request (streaming or normal), it debounce-fetches cloud usage data and
analyzes it. Exposes /usage endpoints for Open WebUI integration.

No periodic polling — only fetches when Ollama is actually used.
Cookie is set via the OLLAMA_COOKIE environment variable.
"""

import os
import logging
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import httpx

from collector import UsageCollector, UsageData
from analyzer import UsageAnalyzer, AnalysisResult

# ── Config ──────────────────────────────────────────────────────────────────

OLLAMA_COOKIE = os.environ.get("OLLAMA_COOKIE", "")
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
DEBOUNCE_SECONDS = int(os.environ.get("OLLAMA_DEBOUNCE_SECONDS", "30"))
HISTORY_PATH = os.environ.get("HISTORY_PATH", "/data/usage_history.json")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
WEBHOOK_ALERTS = os.environ.get(
    "WEBHOOK_ALERTS",
    "session_50,session_75,session_90,hit_limit",
)
WEBHOOK_ALERTS_SET = set(e.strip() for e in WEBHOOK_ALERTS.split(","))
PORT = int(os.environ.get("PORT", "9462"))
HOST = os.environ.get("HOST", "0.0.0.0")

log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("ollama-usage-monitor")

if OLLAMA_COOKIE:
    logger.info("Cookie configured via OLLAMA_COOKIE")
else:
    logger.info("Cookie not configured — set OLLAMA_COOKIE env var")


# ── State ───────────────────────────────────────────────────────────────────

class AppState:
    def __init__(self):
        self.collector = UsageCollector(OLLAMA_COOKIE) if OLLAMA_COOKIE else None
        self.analyzer = UsageAnalyzer(HISTORY_PATH)
        self.last_usage: Optional[UsageData] = None
        self.last_analysis: Optional[AnalysisResult] = None
        self.last_fetch_ts: Optional[str] = None
        self.last_error: Optional[str] = None
        self._last_fetch_time: float = 0.0
        self._proxy_client: Optional[httpx.AsyncClient] = None


state = AppState()

# ── Debounced Usage Fetch ───────────────────────────────────────────────────

async def refresh_usage():
    """Fetch and analyze usage data from ollama.com (runs in thread pool)."""
    if not state.collector:
        state.last_error = "No cookie configured"
        logger.warning("Cannot refresh: OLLAMA_COOKIE not set")
        return

    try:
        loop = asyncio.get_event_loop()
        usage = await loop.run_in_executor(None, state.collector.fetch)

        if usage is None:
            state.last_error = "Failed to fetch usage data"
            logger.error(state.last_error)
            return

        state.last_usage = usage
        state.last_fetch_ts = usage.ts

        analysis = state.analyzer.analyze(usage)
        state.last_analysis = analysis
        state.last_error = None

        logger.info("Usage refreshed: %s", analysis)

        if analysis.should_notify() and WEBHOOK_URL:
            asyncio.create_task(_send_webhook(analysis))

    except Exception as e:
        state.last_error = str(e)
        logger.error("Error during refresh: %s", e)


async def debounced_refresh():
    """Fetch usage, but only if debounce period has elapsed."""
    now = asyncio.get_event_loop().time()
    elapsed = now - state._last_fetch_time
    if elapsed < DEBOUNCE_SECONDS:
        logger.debug(
            "Debounce: skipping usage fetch (%.0fs since last)",
            elapsed,
        )
        return
    state._last_fetch_time = now
    await refresh_usage()


async def _send_webhook(analysis: AnalysisResult):
    """Send a webhook notification if events match configured set."""
    events = set()
    for alert in analysis.alerts:
        if alert in WEBHOOK_ALERTS_SET:
            events.add(alert)
    if analysis.session_will_hit_limit and "hit_limit" in WEBHOOK_ALERTS_SET:
        events.add("hit_limit")

    if not events:
        return

    payload = {
        "events": list(events),
        "analysis": analysis.to_dict(),
        "ts": datetime.now(timezone.utc).isoformat(),
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(WEBHOOK_URL, json=payload)
            resp.raise_for_status()
            logger.info("Webhook sent: %s -> %s", events, resp.status_code)
    except Exception as e:
        logger.error("Webhook failed: %s", e)


# ── FastAPI App ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    state._proxy_client = httpx.AsyncClient(
        base_url=OLLAMA_BASE_URL,
        timeout=600,
    )
    # Initial fetch on startup
    await refresh_usage()
    yield
    await state._proxy_client.aclose()


app = FastAPI(
    title="Ollama Cloud Usage Monitor + Proxy",
    description="Transparent proxy for Ollama API with cloud usage monitoring.",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Streaming Detection ─────────────────────────────────────────────────────

def _is_streaming_request(body: bytes, headers: dict) -> bool:
    """Detect if this is a streaming request (SSE)."""
    if headers.get("accept", "") == "text/event-stream":
        return True
    if body and (
        b'"stream":true' in body or b'"stream": true' in body
    ):
        return True
    return False


# ── Proxy Logic ─────────────────────────────────────────────────────────────

async def _proxy_stream(target_path: str, headers: dict, body: bytes) -> StreamingResponse:
    """Proxy a streaming request — forward SSE chunks as they arrive."""

    async def generate():
        async with state._proxy_client.stream(
            "POST" if body else "GET",
            target_path,
            headers=headers,
            content=body or None,
        ) as resp:
            async for chunk in resp.aiter_bytes():
                yield chunk

        # After stream completes, trigger usage fetch (debounced)
        asyncio.create_task(debounced_refresh())

    return StreamingResponse(generate(), media_type="text/event-stream")


async def _proxy_normal(target_path: str, headers: dict, body: bytes) -> Response:
    """Proxy a normal (non-streaming) request."""
    method = "POST" if body else "GET"
    resp = await state._proxy_client.request(
        method, target_path, headers=headers, content=body or None,
    )

    # After response received, trigger usage fetch (debounced)
    asyncio.create_task(debounced_refresh())

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers=dict(resp.headers),
    )


# ── Monitor Endpoints (defined BEFORE the catch-all proxy) ──────────────────

@app.get("/health")
async def health():
    """Health check endpoint."""
    return {
        "status": "ok" if state.collector else "no_cookie",
        "last_fetch": state.last_fetch_ts,
        "last_error": state.last_error,
        "proxy_to": OLLAMA_BASE_URL,
        "debounce_s": DEBOUNCE_SECONDS,
    }


@app.get("/usage")
async def get_usage():
    """Get current usage data with analysis and prediction."""
    if state.last_usage is None:
        await refresh_usage()
    if state.last_usage is None:
        raise HTTPException(
            status_code=503,
            detail="No usage data available yet. Check OLLAMA_COOKIE config.",
        )
    return {
        "usage": state.last_usage.to_dict(),
        "analysis": state.last_analysis.to_dict() if state.last_analysis else None,
        "last_fetch": state.last_fetch_ts,
        "last_error": state.last_error,
    }


@app.get("/usage/raw")
async def get_raw_usage():
    """Get raw usage data without analysis."""
    if state.last_usage is None:
        await refresh_usage()
    if state.last_usage is None:
        raise HTTPException(status_code=503, detail="No usage data available")
    return state.last_usage.to_dict()


@app.get("/usage/history")
async def get_history():
    """Get usage history (last 20 snapshots)."""
    return {"history": state.analyzer.get_history()}


@app.post("/usage/refresh")
async def force_refresh():
    """Force an immediate refresh of usage data."""
    await refresh_usage()
    if state.last_usage is None:
        raise HTTPException(status_code=503, detail="Refresh failed")
    return {
        "message": "Refresh complete",
        "usage": state.last_usage.to_dict(),
        "analysis": state.last_analysis.to_dict() if state.last_analysis else None,
    }


@app.get("/usage/summary")
async def get_summary():
    """Get a human-readable summary of usage status."""
    if state.last_usage is None:
        await refresh_usage()
    if state.last_usage is None:
        return {"summary": "No data available yet. Check OLLAMA_COOKIE."}

    u = state.last_usage
    a = state.last_analysis

    lines = [
        f"Ollama Cloud ({u.tier})",
        f"Session: {u.session_pct:.1f}% ({u.session_used}/{u.session_limit})",
        f"Weekly:  {u.weekly_pct:.1f}% ({u.weekly_used}/{u.weekly_limit})",
        f"Reset:   in {u.reset_in_min} min",
    ]

    if a:
        if a.burn_rate_per_hour > 0:
            lines.append(f"Burn:    {a.burn_rate_per_hour:.1f}%/h")
        if a.session_will_hit_limit:
            lines.append(
                f"WARNING: Session limit in ~{a.session_time_to_limit_min:.0f}min "
                f"(before reset in {u.reset_in_min}min)!"
            )
        if a.weekly_will_hit_limit:
            lines.append("WARNING: Weekly limit may be reached!")
        if a.alerts:
            for alert in a.alerts:
                pct = alert.split("_")[1]
                lines.append(f"ALERT:   Session at {pct}%")
        lines.append(f"Recommendation: {a.recommendation()}")

    return {"summary": "\n".join(lines)}


# ── Catch-all Proxy (must be LAST — catches everything not matched above) ──

@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def proxy(request: Request, path: str):
    """Transparent proxy: forward any request to the real Ollama server.

    After each response (streaming or not), debounce-fetches cloud usage.
    """
    if not state._proxy_client:
        raise HTTPException(status_code=503, detail="Proxy client not ready")

    target_path = f"/{path}"
    query = request.url.query
    if query:
        target_path += f"?{query}"

    headers = dict(request.headers)
    headers.pop("host", None)

    body = await request.body()

    if _is_streaming_request(body, headers):
        return await _proxy_stream(target_path, headers, body)
    else:
        return await _proxy_normal(target_path, headers, body)


# ── Main ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=HOST, port=PORT, reload=False)