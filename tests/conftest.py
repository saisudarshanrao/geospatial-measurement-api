"""Shared fixtures.

Each test gets an app with its own temporary storage directory, so nothing
leaks between tests and no test writes into the working tree.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        storage_dir=tmp_path / "uploads",
        keep_uploads=True,
        default_strategy="auto",
        max_upload_bytes=8 * 1024 * 1024,
        max_uncompressed_bytes=16 * 1024 * 1024,
        max_archive_entries=100,
        log_level="WARNING",
        cors_origins=["*"],
        default_page_size=100,
        max_page_size=500,
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def upload(client: TestClient):
    """Upload bytes and return the parsed file-info response."""

    def _upload(
        payload: bytes,
        filename: str,
        *,
        strategy: str | None = None,
        expected_status: int = 201,
    ) -> dict:
        params = {"strategy": strategy} if strategy else None
        response = client.post(
            "/api/files/",
            files={"file": (filename, payload, "application/octet-stream")},
            params=params,
        )
        assert response.status_code == expected_status, response.text
        return response.json()

    return _upload
