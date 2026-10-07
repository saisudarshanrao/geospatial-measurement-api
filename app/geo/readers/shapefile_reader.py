"""Shapefile reader, driven by ``pyshp`` over an extracted zip.

A "shapefile" is really a set of sibling files sharing a stem: ``.shp``
(geometry), ``.shx`` (index), ``.dbf`` (attributes), plus the optional ``.prj``
(CRS as WKT) and ``.cpg`` (DBF code page). An upload is therefore an archive,
and one archive may hold several such sets - each is read as its own layer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import shapefile  # pyshp
from pyproj import CRS
from shapely.geometry import shape as shapely_shape

from app.errors import InvalidGeospatialFileError
from app.geo.crs import WGS84, CRSResolutionError, resolve_crs
from app.geo.readers.base import RawFeature, ReadResult, normalise_properties

#: Attribute names commonly used as a stable feature identifier, in priority
#: order. Matched case-insensitively.
ID_FIELD_CANDIDATES = ("id", "fid", "objectid", "gid", "feature_id", "uid", "name")


def _group_layers(root: Path, members: list[Path]) -> dict[str, dict[str, Path]]:
    """Group extracted members by shapefile stem -> {extension: path}."""
    layers: dict[str, dict[str, Path]] = {}
    for relative in members:
        suffix = relative.suffix.lower()
        if suffix not in {".shp", ".shx", ".dbf", ".prj", ".cpg"}:
            continue
        # Key on the directory + stem so two shapefiles with the same name in
        # different folders stay distinct.
        key = str(relative.parent / relative.stem) if str(relative.parent) != "." else relative.stem
        layers.setdefault(key, {})[suffix] = root / relative
    return {key: parts for key, parts in layers.items() if ".shp" in parts}


def _read_encoding(cpg_path: Path | None) -> str:
    """DBF encoding from the ``.cpg`` sidecar, defaulting to UTF-8."""
    if cpg_path is None or not cpg_path.exists():
        return "utf-8"
    try:
        declared = cpg_path.read_text(encoding="ascii", errors="ignore").strip()
    except OSError:
        return "utf-8"
    if not declared:
        return "utf-8"
    # Shapefiles in the wild write both "UTF-8" and bare code page numbers.
    if declared.isdigit():
        return f"cp{declared}"
    return declared


def _read_prj(prj_path: Path | None, warnings: list[str], layer: str) -> tuple[CRS | None, str]:
    if prj_path is None or not prj_path.exists():
        warnings.append(
            f"Layer {layer!r} has no .prj file; assuming EPSG:4326 (WGS 84 longitude/latitude)."
        )
        return WGS84, "assumed_default"
    try:
        text = prj_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError as exc:  # pragma: no cover - unreadable sidecar
        warnings.append(f"Layer {layer!r}: .prj could not be read ({exc}); assuming EPSG:4326.")
        return WGS84, "assumed_default"
    if not text:
        warnings.append(f"Layer {layer!r} has an empty .prj; assuming EPSG:4326.")
        return WGS84, "assumed_default"
    try:
        crs = resolve_crs(text)
    except CRSResolutionError:
        warnings.append(
            f"Layer {layer!r}: .prj content could not be interpreted as a CRS; assuming EPSG:4326."
        )
        return WGS84, "assumed_default"
    return crs, "prj"


def _pick_feature_id(properties: dict[str, Any]) -> str | None:
    lowered = {key.lower(): key for key in properties}
    for candidate in ID_FIELD_CANDIDATES:
        key = lowered.get(candidate)
        if key is not None and properties[key] not in (None, ""):
            return str(properties[key])
    return None


def read_shapefile_zip(root: Path, members: list[Path]) -> ReadResult:
    """Read every shapefile found in an already-extracted archive."""
    warnings: list[str] = []
    layers = _group_layers(root, members)
    if not layers:
        raise InvalidGeospatialFileError(
            "Zip archive contains no .shp file. A shapefile upload must include "
            "at least the .shp, .shx and .dbf components.",
            details={"found": sorted({Path(m).suffix.lower() for m in members if Path(m).suffix})},
        )
    if len(layers) > 1:
        warnings.append(
            f"Archive contains {len(layers)} shapefiles; features from all of them "
            "were read and tagged with their layer name."
        )

    features: list[RawFeature] = []
    layer_names: list[str] = []
    file_crs: CRS | None = None
    file_crs_source = "assumed_default"
    index = 0

    for layer_key in sorted(layers):
        parts = layers[layer_key]
        layer_name = Path(layer_key).name
        layer_names.append(layer_name)

        for required in (".shx", ".dbf"):
            if required not in parts:
                warnings.append(
                    f"Layer {layer_name!r} is missing its {required} component; "
                    "reading what is available."
                )

        crs, crs_source = _read_prj(parts.get(".prj"), warnings, layer_name)
        if file_crs is None:
            file_crs, file_crs_source = crs, crs_source

        encoding = _read_encoding(parts.get(".cpg"))
        reader_kwargs: dict[str, Any] = {"shp": str(parts[".shp"])}
        if ".shx" in parts:
            reader_kwargs["shx"] = str(parts[".shx"])
        if ".dbf" in parts:
            reader_kwargs["dbf"] = str(parts[".dbf"])

        try:
            reader = shapefile.Reader(**reader_kwargs, encoding=encoding, encodingErrors="replace")
        except Exception as exc:
            raise InvalidGeospatialFileError(
                f"Layer {layer_name!r} could not be opened as a shapefile: {exc}"
            ) from exc

        with reader:
            try:
                record_count = len(reader)
            except Exception:  # pragma: no cover - corrupt header
                record_count = 0
            if record_count == 0:
                warnings.append(f"Layer {layer_name!r} contains no features.")

            for position in range(record_count):
                feature_warnings: list[str] = []
                properties: dict[str, Any] = {}
                geometry = None
                error: str | None = None
                try:
                    shape_record = reader.shapeRecord(position)
                    try:
                        properties = normalise_properties(dict(shape_record.record.as_dict()))
                    except Exception as exc:  # attributes missing/corrupt
                        feature_warnings.append(f"Attributes could not be read: {exc}")
                    raw_shape = shape_record.shape
                    if raw_shape is None or raw_shape.shapeType == shapefile.NULL:
                        feature_warnings.append("Shapefile record has a NULL shape.")
                    else:
                        geometry = shapely_shape(raw_shape.__geo_interface__)
                except Exception as exc:
                    # One unreadable record must not lose the other 9,999.
                    error = f"Record could not be read: {exc}"

                features.append(
                    RawFeature(
                        index=index,
                        geometry=geometry,
                        crs=crs,
                        properties=properties,
                        feature_id=_pick_feature_id(properties),
                        layer=layer_name,
                        warnings=feature_warnings,
                        error=error,
                    )
                )
                index += 1

    return ReadResult(
        features=features,
        crs=file_crs,
        crs_source=file_crs_source,
        layers=layer_names,
        warnings=warnings,
    )
