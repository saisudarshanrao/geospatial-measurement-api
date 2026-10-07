"""Upload format detection.

Detection leads with content, not the filename: an extension is a hint a client
controls, while the first bytes of the payload are the thing we are about to
parse. The extension is used only to break the one genuine ambiguity - a zip
archive can hold either a shapefile set or a KMZ's KML document - and even then
the archive listing decides.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from app.errors import EmptyUploadError, UnsupportedFileError
from app.geo.archive import list_archive_names
from app.models import SourceFormat

ZIP_MAGIC = b"PK\x03\x04"
#: Empty and spanned archives start with different signatures.
ZIP_EMPTY_MAGIC = b"PK\x05\x06"
ZIP_SPANNED_MAGIC = b"PK\x07\x08"

SHAPEFILE_MAGIC = b"\x00\x00\x27\x0a"  # big-endian file code 9994

ACCEPTED_EXTENSIONS = (".zip", ".kml", ".kmz")


@dataclass(frozen=True)
class DetectedFormat:
    source_format: SourceFormat
    #: Archive members, when the upload is a zip. Saves re-opening it.
    archive_names: list[str] | None = None
    note: str | None = None


def _looks_like_xml(payload: bytes) -> bool:
    head = payload[:4096].lstrip(b"\xef\xbb\xbf").lstrip()
    return head.startswith(b"<")


def _mentions_kml(payload: bytes) -> bool:
    head = payload[:16384].lower()
    return b"<kml" in head or b"opengis.net/kml" in head or b"earth.google.com/kml" in head


def detect_format(payload: bytes, filename: str | None = None) -> DetectedFormat:
    """Work out what was uploaded, or raise a 400-class error."""
    if not payload:
        raise EmptyUploadError("Uploaded file is empty.")

    extension = PurePosixPath(filename or "").suffix.lower()

    if payload.startswith(ZIP_MAGIC):
        names = list_archive_names(payload)
        lowered = [name.lower() for name in names]
        has_shp = any(name.endswith(".shp") for name in lowered)
        has_kml = any(name.endswith(".kml") for name in lowered)
        if has_shp:
            return DetectedFormat(SourceFormat.SHAPEFILE, archive_names=names)
        if has_kml:
            return DetectedFormat(
                SourceFormat.KMZ,
                archive_names=names,
                note="Archive contains KML rather than a shapefile; read as KMZ.",
            )
        raise UnsupportedFileError(
            "Zip archive contains neither a .shp nor a .kml file.",
            details={"members": names[:20], "member_count": len(names)},
        )

    if payload.startswith((ZIP_EMPTY_MAGIC, ZIP_SPANNED_MAGIC)):
        raise UnsupportedFileError(
            "Upload looks like an empty or multi-part zip archive, which cannot be read."
        )

    if _looks_like_xml(payload) and (_mentions_kml(payload) or extension == ".kml"):
        return DetectedFormat(SourceFormat.KML)

    if payload.startswith(SHAPEFILE_MAGIC) or extension == ".shp":
        raise UnsupportedFileError(
            "A bare .shp file cannot be read on its own: geometry lives in .shp, "
            "attributes in .dbf and the index in .shx. Please upload a .zip "
            "containing the whole shapefile set.",
        )

    if _looks_like_xml(payload):
        raise UnsupportedFileError(
            "Upload is XML but does not look like KML (no <kml> root element found)."
        )

    raise UnsupportedFileError(
        "Unsupported file type. Accepted uploads are a .zip containing a "
        "shapefile, a .kml document, or a .kmz archive.",
        details={"accepted_extensions": list(ACCEPTED_EXTENSIONS)},
    )
