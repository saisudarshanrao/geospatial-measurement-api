"""Persistence boundary.

Two collaborators, both behind a ``Protocol`` so the implementation can change
without touching the routers:

:class:`FileRepository`
    The processed records. The shipped implementation keeps them in memory,
    which is the right default for a stateless measurement service and makes
    the test suite fast; swapping in PostgreSQL means implementing four methods.
:class:`UploadStore`
    The original uploaded bytes on disk, so a file can be re-read later without
    asking the client to upload it again.
"""

from __future__ import annotations

import re
import shutil
import threading
from pathlib import Path
from typing import Protocol

from app.models import UploadedFile

#: Anything outside this set is replaced before a name touches the filesystem.
_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_STORED_NAME = 120


def safe_filename(name: str | None, *, fallback: str = "upload") -> str:
    """A filesystem-safe version of a client-supplied filename."""
    candidate = Path(name or "").name.strip()
    candidate = _UNSAFE_FILENAME.sub("_", candidate).strip("._")
    if not candidate:
        return fallback
    if len(candidate) > _MAX_STORED_NAME:
        suffix = Path(candidate).suffix[:16]
        candidate = candidate[: _MAX_STORED_NAME - len(suffix)] + suffix
    return candidate


class FileRepository(Protocol):
    """Storage for processed file records."""

    def add(self, record: UploadedFile) -> None: ...

    def get(self, file_id: str) -> UploadedFile | None: ...

    def list(self, *, limit: int, offset: int) -> tuple[list[UploadedFile], int]: ...

    def delete(self, file_id: str) -> bool: ...


class InMemoryFileRepository:
    """Thread-safe in-process repository.

    Uvicorn runs request handlers in a thread pool, so the lock is not
    decorative. Insertion order is preserved and listing is newest-first, which
    is what a client paging through recent uploads expects.
    """

    def __init__(self) -> None:
        self._records: dict[str, UploadedFile] = {}
        self._lock = threading.RLock()

    def add(self, record: UploadedFile) -> None:
        with self._lock:
            self._records[record.id] = record

    def get(self, file_id: str) -> UploadedFile | None:
        with self._lock:
            return self._records.get(file_id)

    def list(self, *, limit: int, offset: int) -> tuple[list[UploadedFile], int]:
        with self._lock:
            ordered = sorted(self._records.values(), key=lambda item: item.created_at, reverse=True)
            return ordered[offset : offset + limit], len(ordered)

    def delete(self, file_id: str) -> bool:
        with self._lock:
            return self._records.pop(file_id, None) is not None

    def clear(self) -> None:
        with self._lock:
            self._records.clear()


class UploadStore:
    """Stores original uploads under ``<root>/<file_id>/<filename>``.

    The per-id directory means two uploads called ``survey.kml`` never collide,
    and deleting a record is a single ``rmtree``.
    """

    def __init__(self, root: Path, *, enabled: bool = True) -> None:
        self.root = Path(root)
        self.enabled = enabled

    def save(self, file_id: str, filename: str, payload: bytes) -> Path | None:
        if not self.enabled:
            return None
        directory = self.root / file_id
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / safe_filename(filename)
        target.write_bytes(payload)
        return target

    def delete(self, file_id: str) -> None:
        shutil.rmtree(self.root / file_id, ignore_errors=True)
