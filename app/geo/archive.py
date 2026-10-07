"""Hardened zip extraction.

Uploads are untrusted input, so extraction is bounded on three axes that
``ZipFile.extractall`` does not guard on its own:

* **path traversal** - a member named ``../../etc/cron.d/x`` must not escape
  the destination directory;
* **total uncompressed size** - a zip bomb is a few KB that expands to
  gigabytes, so expansion is capped and checked against the declared size *and*
  the bytes actually written;
* **member count** - a hundred thousand tiny entries is its own denial of
  service.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from app.errors import InvalidGeospatialFileError, UnsupportedFileError

#: Copied in chunks so a member is never held in memory in full.
_CHUNK = 64 * 1024


@dataclass(frozen=True)
class ExtractedArchive:
    root: Path
    #: Destination-relative paths of the files written, in archive order.
    members: list[Path]
    total_bytes: int


def _is_safe_member(name: str) -> bool:
    path = PurePosixPath(name.replace("\\", "/"))
    if path.is_absolute() or name.startswith("/"):
        return False
    if any(part == ".." for part in path.parts):
        return False
    # Windows-style drive letters, e.g. "C:/data/x.shp".
    return not (len(name) > 1 and name[1] == ":")


def extract_archive(
    payload: bytes,
    destination: Path,
    *,
    max_entries: int,
    max_uncompressed_bytes: int,
) -> ExtractedArchive:
    """Extract a zip archive into ``destination``, refusing unsafe content."""
    destination.mkdir(parents=True, exist_ok=True)
    members: list[Path] = []
    written_total = 0

    try:
        archive = zipfile.ZipFile(_as_stream(payload))
    except zipfile.BadZipFile as exc:
        raise UnsupportedFileError("Upload is not a readable zip archive.") from exc

    with archive:
        entries = [info for info in archive.infolist() if not info.is_dir()]
        if not entries:
            raise InvalidGeospatialFileError("Zip archive contains no files.")
        if len(entries) > max_entries:
            raise InvalidGeospatialFileError(
                f"Zip archive contains {len(entries)} files, more than the {max_entries} allowed.",
                details={"entries": len(entries), "limit": max_entries},
            )

        declared = sum(max(info.file_size, 0) for info in entries)
        if declared > max_uncompressed_bytes:
            raise InvalidGeospatialFileError(
                f"Zip archive expands to more than the allowed {max_uncompressed_bytes} bytes.",
                details={"declared_bytes": declared, "limit": max_uncompressed_bytes},
            )

        for info in entries:
            if not _is_safe_member(info.filename):
                raise InvalidGeospatialFileError(
                    "Zip archive contains an unsafe member path.",
                    details={"member": info.filename},
                )
            relative = Path(*PurePosixPath(info.filename.replace("\\", "/")).parts)
            target = destination / relative
            resolved_root = destination.resolve()
            if not str(target.resolve()).startswith(str(resolved_root)):
                raise InvalidGeospatialFileError(
                    "Zip archive contains an unsafe member path.",
                    details={"member": info.filename},
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, open(target, "wb") as sink:
                while chunk := source.read(_CHUNK):
                    written_total += len(chunk)
                    # Re-check while writing: file_size in the header is a
                    # claim, not a guarantee.
                    if written_total > max_uncompressed_bytes:
                        raise InvalidGeospatialFileError(
                            "Zip archive expands to more than the allowed "
                            f"{max_uncompressed_bytes} bytes."
                        )
                    sink.write(chunk)
            members.append(relative)

    return ExtractedArchive(root=destination, members=members, total_bytes=written_total)


def list_archive_names(payload: bytes) -> list[str]:
    """Member names of a zip archive, without extracting anything."""
    try:
        with zipfile.ZipFile(_as_stream(payload)) as archive:
            return [info.filename for info in archive.infolist() if not info.is_dir()]
    except zipfile.BadZipFile as exc:
        raise UnsupportedFileError("Upload is not a readable zip archive.") from exc


def read_archive_member(payload: bytes, name: str, *, max_bytes: int) -> bytes:
    """Bytes of a single archive member, refusing one larger than ``max_bytes``."""
    with zipfile.ZipFile(_as_stream(payload)) as archive:
        info = archive.getinfo(name)
        if info.file_size > max_bytes:
            raise InvalidGeospatialFileError(
                f"Archive member {name!r} expands to more than the allowed {max_bytes} bytes.",
                details={"member": name, "declared_bytes": info.file_size},
            )
        with archive.open(info) as handle:
            data = handle.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise InvalidGeospatialFileError(
                f"Archive member {name!r} expands to more than the allowed {max_bytes} bytes.",
                details={"member": name},
            )
        return data


def _as_stream(payload: bytes):
    from io import BytesIO

    return BytesIO(payload)
