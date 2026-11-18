from collections.abc import AsyncIterator
from uuid import UUID

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import SQLAlchemyError

from app.api import health
from app.main import app

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)

    async with AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as test_client:
        yield test_client


def assert_request_id(response: object) -> None:
    request_id = response.headers["X-Request-ID"]
    UUID(request_id)
    assert response.json().get("error", {}).get("request_id") == request_id


async def test_root_endpoint(client: AsyncClient) -> None:
    response = await client.get("/")

    assert response.status_code == 200
    assert response.json() == {
        "name": "MafundiPro",
        "version": "0.1.0",
        "status": "running",
        "documentation": "/docs",
    }
    assert "X-Request-ID" in response.headers


async def test_api_v1_root_endpoint(client: AsyncClient) -> None:
    response = await client.get("/api/v1/")

    assert response.status_code == 200
    assert response.json() == {
        "name": "MafundiPro API",
        "version": "v1",
        "status": "available",
    }
    assert "X-Request-ID" in response.headers


async def test_liveness_endpoint(client: AsyncClient) -> None:
    response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "alive"}
    assert "X-Request-ID" in response.headers


async def test_readiness_endpoint_with_database(client: AsyncClient) -> None:
    response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "database": "connected",
    }
    assert "X-Request-ID" in response.headers


async def test_readiness_endpoint_when_database_is_unavailable(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def simulate_database_failure() -> None:
        raise SQLAlchemyError("Simulated database connection failure.")

    monkeypatch.setattr(
        health.engine,
        "connect",
        simulate_database_failure,
    )

    response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert response.json()["error"]["message"] == ("The application database is unavailable.")
    assert_request_id(response)


async def test_unknown_route_returns_standard_error(client: AsyncClient) -> None:
    response = await client.get("/route-that-does-not-exist")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"
    assert response.json()["error"]["message"] == ("The requested resource was not found.")
    assert_request_id(response)


async def test_request_ids_are_unique(client: AsyncClient) -> None:
    first = await client.get("/health/live")
    second = await client.get("/health/live")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.headers["X-Request-ID"] != second.headers["X-Request-ID"]
