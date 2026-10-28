"""Safe response/log correlation without changing operation error contracts."""

import asyncio
import logging
from uuid import UUID

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.request_diagnostics import RequestDiagnosticsMiddleware

PREFIXES = ("/api/presales", "/api/parts/master", "/api/parts/suggest", "/api/work-items")
LOGGER = "app.request_diagnostics"


@pytest.fixture(autouse=True)
def capture_diagnostics(monkeypatch, caplog):
    # The shared migration fixture uses fileConfig(), which disables loggers
    # imported while pytest collects tests. Re-enable this logger for assertions.
    monkeypatch.setattr(logging.getLogger(LOGGER), "disabled", False)
    caplog.set_level(logging.WARNING, logger=LOGGER)


class OperationPayload(BaseModel):
    quantity: int


def make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestDiagnosticsMiddleware, path_prefixes=PREFIXES)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["https://frontend.example"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID"],
    )

    @app.get("/api/presales/ok")
    @app.post("/api/parts/master")
    def ok(request: Request):
        return {"request_id": request.state.request_id}

    @app.get("/api/presales/http/{status_code}")
    @app.get("/api/parts/master/http/{status_code}")
    @app.get("/api/parts/suggest/http/{status_code}")
    @app.get("/api/work-items/http/{status_code}")
    def http_error(status_code: int):
        raise HTTPException(
            status_code,
            detail={"code": "BUSINESS_ERROR", "message": "原有业务错误"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    @app.post("/api/presales/validate")
    def validate(payload: OperationPayload):
        return payload

    @app.get("/api/presales/crash/{record_id}")
    def crash(record_id: str):
        raise ValueError("secret SQL parameters and credentials")

    @app.get("/api/presales-other")
    @app.get("/api/parts/master-other")
    @app.get("/api/other")
    def other():
        raise HTTPException(403, detail="outside scope")

    return app


def failure_records(caplog):
    return [record for record in caplog.records if record.name == LOGGER]


@pytest.mark.parametrize(
    "method,path", [("GET", "/api/presales/ok"), ("POST", "/api/parts/master")]
)
def test_server_generated_id_ignores_client_header_and_is_unique(method, path, caplog):
    client = TestClient(make_app())
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        first = client.request(
            method, path, headers={"X-Request-ID": "untrusted-client-id"}
        )
        second = client.request(method, path)
    first_id = first.headers["X-Request-ID"]
    assert UUID(first_id).version == 4
    assert first_id != "untrusted-client-id"
    assert first_id != second.headers["X-Request-ID"]
    assert first.json() == {"request_id": first_id}
    assert not failure_records(caplog)


@pytest.mark.parametrize("status_code", [401, 403, 409, 422, 503])
@pytest.mark.parametrize("prefix", PREFIXES)
def test_handled_http_errors_keep_body_headers_and_log_matching_id(
    prefix, status_code, caplog
):
    response = TestClient(make_app()).get(f"{prefix}/http/{status_code}")
    assert response.status_code == status_code
    assert response.json() == {
        "detail": {"code": "BUSINESS_ERROR", "message": "原有业务错误"}
    }
    assert response.headers["WWW-Authenticate"] == "Bearer"
    [record] = failure_records(caplog)
    assert record.request_id == response.headers["X-Request-ID"]
    assert record.status_code == status_code
    assert record.route == f"{prefix}/http/{{status_code}}"
    assert "原有业务错误" not in record.getMessage()


def test_validation_keeps_fastapi_contract_but_does_not_log_invalid_input(caplog):
    response = TestClient(make_app()).post(
        "/api/presales/validate?token=secret-query",
        json={"quantity": "secret-payload"},
        headers={"Authorization": "Bearer secret-token", "Cookie": "secret-cookie"},
    )
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "quantity"]
    assert response.json()["detail"][0]["input"] == "secret-payload"
    [record] = failure_records(caplog)
    assert record.request_id == response.headers["X-Request-ID"]
    assert "secret" not in record.getMessage()
    assert record.exc_info is None


def test_uncaught_error_has_safe_original_500_body_and_browser_readable_id(caplog):
    response = TestClient(make_app()).get(
        "/api/presales/crash/private-path?password=secret-query",
        headers={"Origin": "https://frontend.example", "Authorization": "secret-token"},
    )
    assert response.status_code == 500
    assert response.text == "Internal Server Error"
    assert response.headers["Access-Control-Allow-Origin"] == "https://frontend.example"
    assert "X-Request-ID" in response.headers["Access-Control-Expose-Headers"]
    [record] = failure_records(caplog)
    assert record.request_id == response.headers["X-Request-ID"]
    assert record.route == "/api/presales/crash/{record_id}"
    assert record.levelno == logging.ERROR
    assert "ValueError" in record.getMessage()
    assert ":crash" in record.getMessage()
    assert "secret" not in record.getMessage()
    assert "private-path" not in record.getMessage()
    assert record.exc_info is None


def test_unmatched_scoped_path_does_not_log_user_input(caplog):
    response = TestClient(make_app()).get("/api/presales/private-path?secret=query")
    assert response.status_code == 404
    [record] = failure_records(caplog)
    assert record.request_id == response.headers["X-Request-ID"]
    assert record.route == "<unmatched>"
    assert "private-path" not in record.getMessage()
    assert "secret" not in record.getMessage()


@pytest.mark.parametrize(
    "path", ["/api/other", "/api/presales-other", "/api/parts/master-other"]
)
def test_non_scoped_routes_keep_existing_behavior(path, caplog):
    response = TestClient(make_app()).get(path)
    assert response.status_code == 403
    assert response.json() == {"detail": "outside scope"}
    assert "X-Request-ID" not in response.headers
    assert not failure_records(caplog)


def test_cancelled_request_propagates_without_fake_500_or_failure_log(caplog):
    messages = []

    async def cancel(scope, receive, send):
        raise asyncio.CancelledError()

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    middleware = RequestDiagnosticsMiddleware(cancel, path_prefixes=PREFIXES)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            middleware({"type": "http", "path": "/api/presales"}, receive, send)
        )
    assert messages == []
    assert not failure_records(caplog)


def test_started_response_error_does_not_send_second_response_or_leak_exception(caplog):
    messages = []

    async def broken_stream(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise ValueError("secret-stream-payload")

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    middleware = RequestDiagnosticsMiddleware(broken_stream, path_prefixes=PREFIXES)
    with pytest.raises(
        RuntimeError, match="Response interrupted; request_id="
    ) as caught:
        asyncio.run(
            middleware({"type": "http", "path": "/api/presales"}, receive, send)
        )
    assert len(messages) == 1
    [record] = failure_records(caplog)
    assert (b"x-request-id", record.request_id.encode()) in messages[0]["headers"]
    assert record.request_id in str(caught.value)
    assert caught.value.__suppress_context__ is True
    assert "secret" not in record.getMessage()


def test_main_hooks_diagnostics_inside_cors():
    from app.main import app

    classes = [middleware.cls for middleware in app.user_middleware]
    assert classes.index(CORSMiddleware) < classes.index(RequestDiagnosticsMiddleware)
    cors = next(
        middleware
        for middleware in app.user_middleware
        if middleware.cls is CORSMiddleware
    )
    assert "X-Request-ID" in cors.kwargs["expose_headers"]
