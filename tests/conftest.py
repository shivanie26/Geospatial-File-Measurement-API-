"""Shared fixtures: every test gets its own temporary storage and its own app instance."""
from __future__ import annotations

from typing import Any, Callable

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

ClientFactory = Callable[..., TestClient]


@pytest.fixture
def storage_dir(tmp_path):
    return tmp_path / "storage"


@pytest.fixture
def make_client(storage_dir) -> ClientFactory:
    """Build a client against ``storage_dir``; keyword arguments override Settings fields.

    ``raise_server_exceptions=False`` lets tests observe the 500 handler instead of
    having Starlette re-raise the error into the test.
    """

    def _make(*, raise_server_exceptions: bool = True, **overrides: Any) -> TestClient:
        values: dict[str, Any] = {"storage_dir": storage_dir, "assume_crs_if_missing": None}
        values.update(overrides)
        # _env_file=None keeps a developer's local .env from changing test behaviour.
        settings = Settings(_env_file=None, **values)
        return TestClient(create_app(settings), raise_server_exceptions=raise_server_exceptions)

    return _make


@pytest.fixture
def client(make_client) -> TestClient:
    return make_client()
