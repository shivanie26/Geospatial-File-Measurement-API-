"""Small request/response helpers shared by the test modules."""
from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient


def upload(client: TestClient, filename: str, content: bytes, content_type: str = "application/octet-stream"):
    return client.post("/api/files/", files={"file": (filename, content, content_type)})


def upload_ok(client: TestClient, filename: str, content: bytes) -> dict:
    response = upload(client, filename, content)
    assert response.status_code == 201, response.text
    return response.json()


def get_measurements(client: TestClient, file_id: str, **params: Any) -> dict:
    response = client.get(f"/api/files/{file_id}/measurements/", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def assert_error(response, status: int, code: str | None = None) -> dict:
    """Every error uses one envelope: {"error": {"code", "message"[, "details"]}}."""
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {"error"}
    assert body["error"]["code"]
    assert body["error"]["message"]
    if code is not None:
        assert body["error"]["code"] == code
    return body["error"]


def by_index(measurements: dict) -> dict[int, dict]:
    return {f["feature_index"]: f for f in measurements["features"]}
