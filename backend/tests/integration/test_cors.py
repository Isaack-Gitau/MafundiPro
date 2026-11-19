import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import settings
from app.main import create_app

pytestmark = pytest.mark.asyncio


async def test_allowed_origin_receives_cors_headers(monkeypatch):
    monkeypatch.setattr(
        settings,
        "frontend_origins",
        "https://trusted.example",
    )
    app = create_app()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get(
            "/",
            headers={"Origin": "https://trusted.example"},
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ("https://trusted.example")
    assert response.headers["access-control-allow-credentials"] == "true"
    assert "x-request-id" in response.headers
    assert "x-request-id" in response.headers["access-control-expose-headers"].lower()


async def test_allowed_origin_accepts_credentialed_preflight(monkeypatch):
    monkeypatch.setattr(
        settings,
        "frontend_origins",
        "https://trusted.example",
    )
    app = create_app()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.options(
            "/api/v1/",
            headers={
                "Origin": "https://trusted.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,x-csrf-token",
            },
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ("https://trusted.example")
    assert response.headers["access-control-allow-credentials"] == "true"

    allowed_methods = response.headers["access-control-allow-methods"].lower()
    allowed_headers = response.headers["access-control-allow-headers"].lower()

    assert "post" in allowed_methods
    assert "content-type" in allowed_headers
    assert "x-csrf-token" in allowed_headers


async def test_disallowed_origin_preflight_is_rejected(monkeypatch):
    monkeypatch.setattr(
        settings,
        "frontend_origins",
        "https://trusted.example",
    )
    app = create_app()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.options(
            "/api/v1/",
            headers={
                "Origin": "https://untrusted.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,x-csrf-token",
            },
        )

    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


async def test_empty_origin_configuration_does_not_allow_browser_origins(
    monkeypatch,
):
    monkeypatch.setattr(settings, "frontend_origins", "")
    app = create_app()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.get(
            "/",
            headers={"Origin": "https://trusted.example"},
        )

    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers
