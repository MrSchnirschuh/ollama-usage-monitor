"""Self-check for the cloud key failover — no network, no test framework.

    python3 test_cloud_failover.py
"""
import asyncio
import os
import sys

os.environ["OLLAMA_CLOUD_KEYS"] = "key-one,key-two"
import httpx  # noqa: E402
import main  # noqa: E402


class FakeClient:
    """Stands in for httpx.AsyncClient: key-one is always saturated."""

    calls: list[str] = []

    def __init__(self, *a, **kw):
        pass

    def build_request(self, method, url, headers=None, content=None):
        self.calls.append(headers["Authorization"])
        return httpx.Request(method, "https://ollama.com" + url, headers=headers, content=content)

    async def send(self, req, stream=False):
        if req.headers["Authorization"] == "Bearer key-one":
            return httpx.Response(429, json={"error": "too many concurrent requests"}, request=req)
        return httpx.Response(200, json={"ok": True}, request=req)

    async def aclose(self):
        pass


async def run():
    main.httpx.AsyncClient = FakeClient
    main.debounced_refresh = lambda: asyncio.sleep(0)

    r1 = await main._cloud_proxy("/v1/chat/completions", {}, b'{"model":"x:cloud"}', False)
    assert r1.status_code == 200, r1.status_code
    assert FakeClient.calls == ["Bearer key-one", "Bearer key-two"], FakeClient.calls
    assert main._preferred_key == 1, "sticky preference not set"

    # Second request must not pay the 429 round trip again.
    r2 = await main._cloud_proxy("/v1/chat/completions", {}, b'{"model":"x:cloud"}', False)
    assert r2.status_code == 200
    assert FakeClient.calls[-1] == "Bearer key-two" and len(FakeClient.calls) == 3, FakeClient.calls

    # Both keys saturated -> the upstream error is passed through.
    original = main.CLOUD_KEYS
    main.CLOUD_KEYS = ["key-one"]
    main._preferred_key = 0
    r3 = await main._cloud_proxy("/v1/chat/completions", {}, b'{"model":"x:cloud"}', False)
    assert r3.status_code == 429, r3.status_code
    main.CLOUD_KEYS = original

    assert main._is_cloud_model(b'{"model":"deepseek-v4.1-flash:cloud"}')
    assert not main._is_cloud_model(b'{"model":"qwen3.5:4b"}')
    assert not main._is_cloud_model(b"")
    print("ok — failover, stickiness, passthrough, model detection")


if __name__ == "__main__":
    asyncio.run(run())
    sys.exit(0)
