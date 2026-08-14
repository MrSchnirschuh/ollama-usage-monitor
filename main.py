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
import json

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
        # Per-model accumulated GPU time (ns) from eval_duration of done:true chunks
        self.gpu_ns: dict[str, int] = {}


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


def _accumulate_gpu_time(chunk: bytes) -> None:
    """Accumulate GPU time per model from done:true chunks in the response stream.

    Ollama's streaming responses end with a `done:true` chunk carrying
    `model` and `eval_duration` (ns) — the actual GPU compute time.
    Handles both NDJSON (Ollama /api/chat) and SSE (OpenAI /v1/...).
    """
    # ponytail: O(n) str ops on small chunks; fine at proxy throughput
    text = chunk.decode("utf-8", errors="ignore")
    lines = [text] if "\n" not in text else text.splitlines()
    for line in lines:
        s = line
        if s.startswith("data: "):
            s = s[6:]
        s = s.strip()
        if not s or s == "[DONE]":
            continue
        try:
            obj = json.loads(s)
        except (json.JSONDecodeError, ValueError):
            continue
        if obj.get("done") is True and obj.get("model") and obj.get("eval_duration"):
            state.gpu_ns[obj["model"]] = state.gpu_ns.get(obj["model"], 0) + int(obj["eval_duration"])


# ── Proxy Logic ─────────────────────────────────────────────────────────────

async def _proxy_stream(target_path: str, headers: dict, body: bytes) -> StreamingResponse:
    """Proxy a streaming request — forward raw bytes as-is from Ollama."""

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

    # Use Ollama's actual content-type from the response headers
    # (NDJSON for /api/chat, SSE for /v1/...)
    async def generate_with_ct():
        nonlocal resp
        async with state._proxy_client.stream(
            "POST" if body else "GET",
            target_path,
            headers=headers,
            content=body or None,
        ) as r:
            resp = r
            async for chunk in r.aiter_bytes():
                _accumulate_gpu_time(chunk)
                yield chunk

        # After stream completes, trigger usage fetch (debounced)
        asyncio.create_task(debounced_refresh())

    resp = None
    gen = generate_with_ct()
    # Prime the generator to get the response object
    try:
        first_chunk = await gen.__anext__()
    except StopAsyncIteration:
        return StreamingResponse(gen, media_type="text/event-stream")

    ct = resp.headers.get("content-type", "text/event-stream") if resp else "text/event-stream"

    async def final_gen():
        yield first_chunk
        async for chunk in gen:
            yield chunk

    return StreamingResponse(final_gen(), media_type=ct)


async def _proxy_normal(target_path: str, headers: dict, body: bytes) -> Response:
    """Proxy a normal (non-streaming) request."""
    method = "POST" if body else "GET"
    resp = await state._proxy_client.request(
        method, target_path, headers=headers, content=body or None,
    )

    # After response received, trigger usage fetch (debounced)
    asyncio.create_task(debounced_refresh())

    # Strip conflicting headers from response — the real Ollama sometimes
    # sends both content-length and transfer-encoding: chunked, which is
    # invalid HTTP/1.1 and causes 400 errors in some clients (Open WebUI).
    clean_headers = dict(resp.headers)
    clean_headers.pop("content-length", None)
    clean_headers.pop("transfer-encoding", None)

    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers=clean_headers,
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


@app.get("/info")
async def get_info():
    """List all available commands and endpoints."""
    return {
        "commands": {
            "usage": "Show detailed usage data with analysis",
            "summary": "Show human-readable usage summary",
            "history": "Show last 20 usage snapshots",
            "refresh": "Force an immediate refresh of usage data",
            "info": "Show this help message",
            "help": "Alias for info",
        },
        "endpoints": {
            "GET /health": "Health check",
            "GET /usage": "Full usage data with analysis",
            "GET /usage/raw": "Raw usage data without analysis",
            "GET /usage/summary": "Human-readable summary",
            "GET /usage/history": "Last 20 usage snapshots",
            "GET /info": "Available commands and endpoints",
            "POST /usage/refresh": "Force refresh usage data",
        },
        "chat_keywords": (
            "Send a chat message with one of these words as the only content "
            "to any model via the proxy: usage, summary, history, refresh, info, help"
        ),
    }


# ── Chat Keyword Filter ──────────────────────────────────────────────────────

_USAGE_KEYWORDS = {"usage", "summary", "history", "refresh", "info", "help"}


def _extract_text_content(content) -> str:
    """Normalize a message content field to plain text.

    Handles:
      - plain string
      - OpenAI vision/tool format: list of {"type": "text", "text": "..."}
    Returns the concatenated text or an empty string.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return ""


def _check_chat_keyword(body: bytes) -> str | None:
    """Check if a chat request body contains a usage keyword.

    Only checks the LAST user message so previous keyword messages
    in the conversation history don't poison detection.
    Returns the keyword if found, None otherwise.
    """
    if not body:
        return None
    try:
        data = json.loads(body)
        messages = data.get("messages", [])
        # Only check the last user message — history messages with keywords
        # (e.g. a previous "info" call) would match first and poison detection
        for msg in reversed(messages):
            role = msg.get("role", "")
            if role != "user":
                continue
            content = _extract_text_content(msg.get("content", ""))
            stripped = content.lower().strip()
            if stripped in _USAGE_KEYWORDS:
                return stripped
            break  # only the last user message
    except (json.JSONDecodeError, TypeError, AttributeError):
        pass
    return None


def _strip_keyword_messages(body: bytes) -> bytes:
    """Remove user messages that are only a usage keyword from the chat history.

    This prevents keyword commands from leaking into the model's context
    on subsequent normal requests.
    """
    if not body:
        return body
    try:
        data = json.loads(body)
        messages = data.get("messages", [])
        if not messages:
            return body
        filtered = [
            m for m in messages
            if not (
                m.get("role") == "user"
                and isinstance(m.get("content"), str)
                and m["content"].lower().strip() in _USAGE_KEYWORDS
            )
        ]
        if len(filtered) == len(messages):
            return body  # nothing changed
        data["messages"] = filtered
        return json.dumps(data, ensure_ascii=False).encode("utf-8")
    except (json.JSONDecodeError, TypeError, AttributeError):
        return body


# ── Catch-all Proxy (must be LAST — catches everything not matched above) ──

@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def proxy(request: Request, path: str):
    """Transparent proxy: forward any request to the real Ollama server.

    After each response (streaming or not), debounce-fetches cloud usage.
    Chat messages containing usage keywords are intercepted and answered directly.
    """
    try:
        if not state._proxy_client:
            raise HTTPException(status_code=503, detail="Proxy client not ready")

        # Remember original path BEFORE any rewrite — needed for keyword
        # response format detection (Ollama vs OpenAI).
        original_path = path

        # Route Ollama-format chat to the native /api/chat endpoint so the
        # client gets NDJSON (raw JSON lines). Handles /v1/api/chat and api/chat.
        # DO NOT rewrite to /v1/chat/completions: that makes the upstream return
        # SSE (data: {...}), which NDJSON-expecting clients fail to parse.
        if path == "api/chat" or path.endswith("/api/chat"):
            path = "api/chat"

        target_path = f"/{path}"
        query = request.url.query
        if query:
            target_path += f"?{query}"

        headers = dict(request.headers)
        headers.pop("host", None)
        headers.pop("content-length", None)
        headers.pop("transfer-encoding", None)

        body = await request.body()

        # ── Chat keyword interception ─────────────────────────────────────────
        # Detect format from ORIGINAL path (before rewrite), so Hermes with
        # type=ollama (sending to /api/chat) gets Ollama-format keyword responses.
        # Use endswith to handle paths with /v1/ prefix (e.g. v1/api/chat)
        is_ollama_chat = original_path == "api/chat" or original_path.endswith("/api/chat")
        is_openai_chat = original_path == "v1/chat/completions" or original_path.endswith("/v1/chat/completions")
        if (is_ollama_chat or is_openai_chat) and body:
            keyword = _check_chat_keyword(body)
            if keyword:
                logger.info("Chat keyword '%s' detected — intercepting %s request", keyword, "OpenAI" if is_openai_chat else "Ollama")
                if state.last_usage is None:
                    await refresh_usage()

                # Build formatted text response (no raw JSON in chat output)
                if keyword == "usage":
                    if state.last_usage is None:
                        content = "No usage data available yet."
                    else:
                        u = state.last_usage
                        a = state.last_analysis
                        lines = [
                            f"Ollama Cloud ({u.tier})",
                            f"Session: {u.session_pct:.1f}% ({u.session_used}/{u.session_limit}, {u.session_limit - u.session_used} remaining)",
                            f"Weekly:  {u.weekly_pct:.1f}% ({u.weekly_used}/{u.weekly_limit}, {u.weekly_limit - u.weekly_used} remaining)",
                            f"Reset:   in {u.reset_in_min} min (at {u.reset_at})",
                        ]
                        if a:
                            if a.burn_rate_per_hour > 0:
                                lines.append(f"Burn:    {a.burn_rate_per_hour:.1f}%/h")
                            if a.session_will_hit_limit:
                                lines.append(f"WARNING: Session limit in ~{a.session_time_to_limit_min:.0f}min (before reset in {u.reset_in_min}min)!")
                            if a.weekly_will_hit_limit:
                                lines.append("WARNING: Weekly limit may be reached!")
                            if a.alerts:
                                for alert in a.alerts:
                                    pct = alert.split("_")[1]
                                    lines.append(f"ALERT:   Session at {pct}%")
                            lines.append(f"Recommendation: {a.recommendation()}")
                        if u.models:
                            lines.append("Models:")
                            total_gpu = sum(state.gpu_ns.values()) or 1
                            # GPU time per request, normalized to cheapest = 1x
                            per_req = {
                                m.get("name", "?"): state.gpu_ns.get(m.get("name", "?"), 0) / m.get("requests", 0)
                                for m in u.models
                                if m.get("requests", 0) > 0 and state.gpu_ns.get(m.get("name", "?"), 0) > 0
                            }
                            base = min(per_req.values()) if per_req else 1
                            for m in u.models:
                                pct = m.get("pct", 0)
                                name = m.get("name", "?")
                                reqs = m.get("requests", 0)
                                gpu = state.gpu_ns.get(name, 0)
                                gshare = gpu / total_gpu * 100 if gpu else 0.0
                                x = per_req.get(name, 0) / base if per_req.get(name, 0) else 0.0
                                lines.append(
                                    f"  {name}: {pct:.1f}% ({reqs} req, "
                                    f"GPU {gshare:.1f}% of used, {x:.1f}x)"
                                )
                        content = "\n".join(lines)
                elif keyword == "summary":
                    if state.last_usage is None:
                        content = "No data available yet."
                    else:
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
                                lines.append(f"WARNING: Session limit in ~{a.session_time_to_limit_min:.0f}min (before reset in {u.reset_in_min}min)!")
                            if a.weekly_will_hit_limit:
                                lines.append("WARNING: Weekly limit may be reached!")
                            if a.alerts:
                                for alert in a.alerts:
                                    pct = alert.split("_")[1]
                                    lines.append(f"ALERT:   Session at {pct}%")
                            lines.append(f"Recommendation: {a.recommendation()}")
                        content = "\n".join(lines)
                elif keyword == "history":
                    history = state.analyzer.get_history()
                    if not history:
                        content = "No history available yet."
                    else:
                        lines = [f"Last {len(history)} usage snapshots:"]
                        for i, entry in enumerate(history, 1):
                            ts = entry.get("timestamp", "?")
                            s = entry.get("session_pct", "?")
                            w = entry.get("weekly_pct", "?")
                            lines.append(f"  {i}. {ts} — Session {s}% / Weekly {w}%")
                        content = "\n".join(lines)
                elif keyword == "refresh":
                    await refresh_usage()
                    if state.last_usage is None:
                        content = "Refresh failed — no usage data available."
                    else:
                        u = state.last_usage
                        content = (
                            f"Refresh complete.\n"
                            f"Session: {u.session_pct:.1f}% ({u.session_used}/{u.session_limit})\n"
                            f"Weekly:  {u.weekly_pct:.1f}% ({u.weekly_used}/{u.weekly_limit})"
                        )
                elif keyword in ("info", "help"):
                    content = (
                        "Available commands (send as chat message):\n"
                        "  usage    — Show detailed usage data with analysis\n"
                        "  summary  — Show human-readable usage summary\n"
                        "  history  — Show last 20 usage snapshots\n"
                        "  refresh  — Force an immediate refresh of usage data\n"
                        "  info     — Show this help message\n"
                        "  help     — Alias for info\n"
                        "\n"
                        "HTTP endpoints:\n"
                        "  GET /health          — Health check\n"
                        "  GET /usage           — Full usage data with analysis\n"
                        "  GET /usage/raw       — Raw usage data without analysis\n"
                        "  GET /usage/summary   — Human-readable summary\n"
                        "  GET /usage/history   — Last 20 usage snapshots\n"
                        "  GET /info            — Available commands and endpoints\n"
                        "  POST /usage/refresh  — Force refresh usage data"
                    )

                if is_openai_chat:
                    # Build SSE chunks for streaming requests, plain JSON for non-streaming
                    if _is_streaming_request(body, headers):
                        # OpenAI SSE format: data: {...}\n\n
                        sse_chunks = []
                        # Single chunk with full content
                        chunk = {
                            "id": "usage-monitor",
                            "object": "chat.completion.chunk",
                            "created": int(datetime.now(timezone.utc).timestamp()),
                            "model": "usage-monitor",
                            "choices": [{
                                "index": 0,
                                "delta": {"role": "assistant", "content": content},
                                "finish_reason": None,
                            }],
                        }
                        sse_chunks.append(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n")
                        # Done chunk
                        done = {
                            "id": "usage-monitor",
                            "object": "chat.completion.chunk",
                            "created": int(datetime.now(timezone.utc).timestamp()),
                            "model": "usage-monitor",
                            "choices": [{
                                "index": 0,
                                "delta": {},
                                "finish_reason": "stop",
                            }],
                        }
                        sse_chunks.append(f"data: {json.dumps(done, ensure_ascii=False)}\n\n")
                        sse_chunks.append("data: [DONE]\n\n")
                        return Response(
                            content="".join(sse_chunks),
                            media_type="text/event-stream",
                        )
                    else:
                        chat_response = {
                            "id": "usage-monitor",
                            "object": "chat.completion",
                            "created": int(datetime.now(timezone.utc).timestamp()),
                            "model": "usage-monitor",
                            "choices": [{
                                "index": 0,
                                "message": {"role": "assistant", "content": content},
                                "finish_reason": "stop",
                            }],
                            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                        }
                        return Response(
                            content=json.dumps(chat_response, ensure_ascii=False),
                            media_type="application/json",
                        )
                else:
                    # Ollama format — also handle streaming vs non-streaming
                    if _is_streaming_request(body, headers):
                        # Ollama NDJSON format: {...}\n (raw JSON lines, no data: prefix)
                        ndjson_lines = []
                        chunk = {
                            "model": "usage-monitor",
                            "created_at": datetime.now(timezone.utc).isoformat(),
                            "message": {"role": "assistant", "content": content},
                            "done": False,
                        }
                        ndjson_lines.append(json.dumps(chunk, ensure_ascii=False) + "\n")
                        done = {
                            "model": "usage-monitor",
                            "created_at": datetime.now(timezone.utc).isoformat(),
                            "message": {"role": "assistant", "content": ""},
                            "done": True,
                        }
                        ndjson_lines.append(json.dumps(done, ensure_ascii=False) + "\n")
                        return Response(
                            content="".join(ndjson_lines),
                            media_type="application/x-ndjson",
                        )
                    else:
                        chat_response = {
                            "model": "usage-monitor",
                            "created_at": datetime.now(timezone.utc).isoformat(),
                            "message": {"role": "assistant", "content": content},
                            "done": True,
                        }
                        return Response(
                            content=json.dumps(chat_response, ensure_ascii=False),
                            media_type="application/json",
                        )

        # Strip keyword-only user messages from history before forwarding
        body = _strip_keyword_messages(body)

        if _is_streaming_request(body, headers):
            return await _proxy_stream(target_path, headers, body)
        else:
            return await _proxy_normal(target_path, headers, body)
    except Exception as e:
        logger.error("Proxy error: %s", str(e), exc_info=True)
        raise


# ── Main ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=HOST, port=PORT, reload=False)
