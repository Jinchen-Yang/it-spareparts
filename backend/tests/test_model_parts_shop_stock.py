import asyncio
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from app.services import model_parts_stock
from app.services.model_parts_stock import enrich_model_parts_stock

TOKEN = "synthetic-stock-reader-only-credential"
URL = "https://shop.example.test/api/integrations/model-parts/v1/stock"


def settings(url=URL, token=TOKEN, catalog_hash=""):
    return SimpleNamespace(
        model_parts_shop_stock_url=url,
        model_parts_shop_stock_token=SecretStr(token),
        model_parts_catalog_token_sha256=SecretStr(catalog_hash),
    )


def data(ids):
    return {"revision": 12, "parts": [{"id": i, "good": None, "bad": None} for i in ids]}


def test_stock_enrichment_joins_by_stable_id_in_batches_and_preserves_catalog():
    seen = []

    def read(request):
        assert request.method == "POST" and str(request.url) == URL
        assert request.headers["authorization"] == "Bearer " + TOKEN
        assert "cookie" not in request.headers
        ids = json.loads(request.content)["partIds"]
        seen.append(len(ids))
        return httpx.Response(
            200,
            json={
                "items": [{"partId": i, "good": int(i), "bad": 0} for i in reversed(ids)],
                "asOf": "2026-11-26T10:00:00Z" if len(seen) == 1 else "2026-11-26T10:00:01Z",
            },
        )

    source = data([str(i) for i in range(1, 1002)])
    updated, status, stamp = enrich_model_parts_stock(
        source,
        settings=settings(),
        transport=httpx.MockTransport(read),
    )
    assert seen == [1000, 1] and updated["revision"] == 12 and status == "live"
    assert stamp.startswith("2026-11-26T10:00:00")
    assert all(p["good"] == int(p["id"]) and p["bad"] == 0 for p in updated["parts"])
    assert all(p["good"] is None and p["bad"] is None for p in source["parts"])


@pytest.mark.parametrize(
    "result",
    [
        {"items": [], "asOf": "2026-11-26T10:00:00Z"},
        {"items": [{"partId": "1", "good": -1, "bad": 0}], "asOf": "2026-11-26T10:00:00Z"},
        {"items": [{"partId": "1", "good": 1, "bad": 0}], "asOf": "2026-11-26T10:00:00"},
        {"items": [{"partId": "2", "good": 1, "bad": 0}], "asOf": "2026-11-26T10:00:00Z"},
        {"items": [{"partId": "1", "good": True, "bad": 0}], "asOf": "2026-11-26T10:00:00Z"},
        {
            "items": [{"partId": "1", "good": 1, "bad": None, "cost": 5}],
            "asOf": "2026-11-26T10:00:00Z",
        },
    ],
)
def test_malformed_or_incomplete_stock_never_becomes_zero(result):
    source = data(["1"])
    source["parts"][0]["good"] = 99
    updated, status, stamp = enrich_model_parts_stock(
        source,
        settings=settings(),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=result)),
    )
    assert (status, stamp) == ("unavailable", None)
    assert updated["parts"] == [{"id": "1", "good": None, "bad": None}]


def test_stock_config_and_redirects_do_not_forward_identity_to_other_origins():
    calls = []

    def redirect(request):
        calls.append(str(request.url))
        return httpx.Response(307, headers={"Location": "https://other.example.test/log"})

    for conf in (
        settings(),
        settings(url=""),
        settings(url="http://shop.example.test/stock"),
        settings(url="https://shop.example.test:bad-port/stock"),
        settings(url="https://shop.example.test/read?q=1"),
        settings(url="https://secret@shop.example.test/read"),
        settings(catalog_hash=hashlib.sha256(TOKEN.encode()).hexdigest()),
    ):
        _, status, stamp = enrich_model_parts_stock(
            data(["1"]),
            settings=conf,
            transport=httpx.MockTransport(redirect),
        )
        assert (status, stamp) == ("unavailable", None)
    assert calls == [URL]


def test_partial_failure_discards_the_other_batch():
    def read(request):
        ids = json.loads(request.content)["partIds"]
        if len(ids) < 1000:
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={
                "asOf": "2026-11-26T10:00:00Z",
                "items": [{"partId": i, "good": 0, "bad": 1} for i in ids],
            },
        )

    source = data([str(i) for i in range(1, 1002)])
    result, status, stamp = enrich_model_parts_stock(
        source,
        settings=settings(),
        transport=httpx.MockTransport(read),
    )
    assert (status, stamp) == ("unavailable", None)
    assert all(p["good"] is None and p["bad"] is None for p in result["parts"])


def test_oversize_or_slow_stock_stream_is_unavailable(monkeypatch):
    class Stream(httpx.AsyncByteStream):
        def __init__(self, delay):
            self.delay = delay

        async def __aiter__(self):
            for _ in range(6):
                await asyncio.sleep(self.delay)
                yield b"0" * 16

    monkeypatch.setattr(model_parts_stock, "MAX_REPLY_BYTES", 36)
    for delay, timeout in [(0.0, 1.0), (0.05, 0.01)]:
        monkeypatch.setattr(model_parts_stock, "TIMEOUT", timeout)
        result, status, stamp = enrich_model_parts_stock(
            data(["1"]),
            settings=settings(),
            transport=httpx.MockTransport(
                lambda _, delay=delay: httpx.Response(200, stream=Stream(delay))
            ),
        )
        assert (status, stamp) == ("unavailable", None)
        assert result["parts"][0]["good"] is None
