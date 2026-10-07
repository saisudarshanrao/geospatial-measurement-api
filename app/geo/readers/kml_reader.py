"""KML/KMZ reader built on ``lxml``.

Two things shape this implementation:

**Namespaces.** Files in the wild declare ``http://www.opengis.net/kml/2.2``,
the older ``http://earth.google.com/kml/2.x``, or nothing at all. Elements are
therefore matched on local name, so every variant parses with one code path.

**CRS.** The KML specification (OGC 07-147r2, §6.2) fixes coordinates as
longitude, latitude and optional altitude on WGS 84 - there is no CRS element
to read. So EPSG:4326 is not a guess here, it is the format definition, which
is exactly why these files must be reprojected before measuring.

The parser is also configured to refuse external entities and network access:
an uploaded XML document is untrusted input, and the default lxml settings
would make billion-laughs and file-disclosure attacks available to any caller.
"""

from __future__ import annotations

from typing import Any

from lxml import etree

from app.errors import InvalidGeospatialFileError
from app.geo.archive import list_archive_names, read_archive_member
from app.geo.crs import WGS84
from app.geo.readers.base import RawFeature, ReadResult, normalise_properties

GEOMETRY_TAGS = {
    "Point",
    "LineString",
    "LinearRing",
    "Polygon",
    "MultiGeometry",
    "Track",  # gx:Track
}
#: KML features we recognise but cannot turn into a geometry.
UNSUPPORTED_GEOMETRY_TAGS = {"Model", "PhotoOverlay", "GroundOverlay", "ScreenOverlay"}

CRS_SOURCE = "kml_specification"


def _parser() -> etree.XMLParser:
    return etree.XMLParser(
        resolve_entities=False,  # blocks XXE / billion laughs
        no_network=True,
        load_dtd=False,
        dtd_validation=False,
        huge_tree=False,
        recover=False,
    )


def _local(tag: Any) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _children(element: etree._Element, name: str) -> list[etree._Element]:
    return [child for child in element if _local(child.tag) == name]


def _first(element: etree._Element, name: str) -> etree._Element | None:
    for child in element:
        if _local(child.tag) == name:
            return child
    return None


def _text(element: etree._Element | None) -> str | None:
    if element is None:
        return None
    text = "".join(element.itertext())
    text = text.strip()
    return text or None


def _parse_coordinates(text: str | None) -> list[tuple[float, float]]:
    """Parse a KML ``<coordinates>`` body into 2D tuples.

    The body is whitespace-separated ``lon,lat[,alt]`` triples. Altitude is
    dropped: measurements are planar, and keeping Z here only to discard it in
    the measurement step invites inconsistency.
    """
    if not text:
        return []
    coordinates: list[tuple[float, float]] = []
    for token in text.replace("\n", " ").replace("\t", " ").split():
        parts = token.split(",")
        if len(parts) < 2:
            continue
        try:
            lon = float(parts[0])
            lat = float(parts[1])
        except ValueError:
            continue
        coordinates.append((lon, lat))
    return coordinates


def _parse_track(element: etree._Element) -> list[tuple[float, float]]:
    """``gx:Track`` stores positions as space-separated ``<gx:coord>`` values."""
    coordinates: list[tuple[float, float]] = []
    for coord in _children(element, "coord"):
        parts = (_text(coord) or "").split()
        if len(parts) < 2:
            continue
        try:
            coordinates.append((float(parts[0]), float(parts[1])))
        except ValueError:
            continue
    return coordinates


def _ring_coordinates(boundary: etree._Element | None) -> list[tuple[float, float]]:
    if boundary is None:
        return []
    ring = _first(boundary, "LinearRing")
    target = ring if ring is not None else boundary
    return _parse_coordinates(_text(_first(target, "coordinates")))


def _build_geometry(element: etree._Element):
    """Build a shapely geometry from a KML geometry element.

    Imported lazily per call site to keep this module importable without
    shapely at documentation-generation time.
    """
    from shapely.geometry import (
        GeometryCollection,
        LinearRing,
        LineString,
        MultiLineString,
        MultiPoint,
        MultiPolygon,
        Point,
        Polygon,
    )

    tag = _local(element.tag)

    if tag == "Point":
        coordinates = _parse_coordinates(_text(_first(element, "coordinates")))
        if not coordinates:
            raise ValueError("Point has no usable coordinates.")
        return Point(coordinates[0])

    if tag in {"LineString", "Track"}:
        coordinates = (
            _parse_track(element)
            if tag == "Track"
            else _parse_coordinates(_text(_first(element, "coordinates")))
        )
        if len(coordinates) < 2:
            raise ValueError(f"{tag} needs at least 2 coordinates, found {len(coordinates)}.")
        return LineString(coordinates)

    if tag == "LinearRing":
        coordinates = _parse_coordinates(_text(_first(element, "coordinates")))
        if len(coordinates) < 4:
            raise ValueError(f"LinearRing needs at least 4 coordinates, found {len(coordinates)}.")
        return LinearRing(coordinates)

    if tag == "Polygon":
        shell = _ring_coordinates(_first(element, "outerBoundaryIs"))
        if len(shell) < 4:
            raise ValueError(
                f"Polygon outer boundary needs at least 4 coordinates, found {len(shell)}."
            )
        holes = [
            ring
            for ring in (
                _ring_coordinates(inner) for inner in _children(element, "innerBoundaryIs")
            )
            if len(ring) >= 4
        ]
        return Polygon(shell, holes)

    if tag == "MultiGeometry":
        parts = []
        errors = []
        for child in element:
            if _local(child.tag) not in GEOMETRY_TAGS:
                continue
            try:
                parts.append(_build_geometry(child))
            except ValueError as exc:
                errors.append(str(exc))
        if not parts:
            raise ValueError(
                "MultiGeometry contains no usable geometry"
                + (f" ({'; '.join(errors)})" if errors else "")
                + "."
            )
        kinds = {part.geom_type for part in parts}
        if kinds == {"Point"}:
            return MultiPoint([part.coords[0] for part in parts])
        if kinds <= {"LineString", "LinearRing"}:
            return MultiLineString([list(part.coords) for part in parts])
        if kinds == {"Polygon"}:
            return MultiPolygon(parts)
        # Mixed content is legal KML; a collection is the honest mapping.
        return GeometryCollection(parts)

    raise ValueError(f"Unsupported KML geometry element {tag!r}.")


def _extended_data(placemark: etree._Element) -> dict[str, Any]:
    """Flatten ``<ExtendedData>`` into plain key/value pairs."""
    properties: dict[str, Any] = {}
    extended = _first(placemark, "ExtendedData")
    if extended is None:
        return properties

    for data in _children(extended, "Data"):
        name = data.get("name")
        if not name:
            continue
        properties[name] = _text(_first(data, "value"))

    for schema_data in _children(extended, "SchemaData"):
        for simple in _children(schema_data, "SimpleData"):
            name = simple.get("name")
            if name:
                properties[name] = _text(simple)

    # Some producers put a bare namespaced payload inside ExtendedData.
    for child in extended:
        local = _local(child.tag)
        if local in {"Data", "SchemaData"}:
            continue
        value = _text(child)
        if value is not None:
            properties.setdefault(local, value)

    return properties


def _container_path(placemark: etree._Element) -> str | None:
    """``Document/Folder`` names above a placemark, joined with ``/``."""
    names: list[str] = []
    parent = placemark.getparent()
    while parent is not None:
        if _local(parent.tag) in {"Folder", "Document"}:
            name = _text(_first(parent, "name"))
            if name:
                names.append(name)
        parent = parent.getparent()
    return "/".join(reversed(names)) or None


def _placemark_properties(placemark: etree._Element) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    for field in ("name", "description", "address", "phoneNumber", "snippet", "styleUrl"):
        value = _text(_first(placemark, field))
        if value is not None:
            properties[field] = value
    properties.update(_extended_data(placemark))
    return normalise_properties(properties)


def read_kml(payload: bytes, *, filename: str | None = None) -> ReadResult:
    """Read a KML document into features."""
    warnings: list[str] = []
    try:
        root = etree.fromstring(payload, parser=_parser())
    except etree.XMLSyntaxError as strict_error:
        # Malformed-but-recoverable KML is common enough that refusing it
        # outright would be unhelpful; we recover and say so.
        try:
            recovering = etree.XMLParser(
                resolve_entities=False,
                no_network=True,
                load_dtd=False,
                dtd_validation=False,
                huge_tree=False,
                recover=True,
            )
            root = etree.fromstring(payload, parser=recovering)
            if root is None:
                raise etree.XMLSyntaxError("empty document", None, 0, 0)
            warnings.append(
                f"KML is not well-formed and was parsed in recovery mode ({strict_error}); "
                "some features may be missing."
            )
        except etree.XMLSyntaxError as exc:
            raise InvalidGeospatialFileError(f"File is not valid XML/KML: {exc}") from exc

    recovered = any("recovery mode" in warning for warning in warnings)
    placemarks = [element for element in root.iter() if _local(element.tag) == "Placemark"]
    if not placemarks:
        if recovered:
            # Strict parsing failed *and* recovery salvaged nothing: this is a
            # corrupt document, not a deliberately empty one.
            raise InvalidGeospatialFileError(
                "File is not valid KML: it could only be parsed in recovery mode "
                "and no Placemark elements could be salvaged."
            )
        # A well-formed but empty KML is not a client error.
        warnings.append("KML contains no Placemark elements.")

    features: list[RawFeature] = []
    layers: list[str] = []

    for index, placemark in enumerate(placemarks):
        feature_warnings: list[str] = []
        properties = _placemark_properties(placemark)
        layer = _container_path(placemark)
        if layer and layer not in layers:
            layers.append(layer)

        geometry = None
        error: str | None = None
        geometry_element = next(
            (child for child in placemark.iter() if _local(child.tag) in GEOMETRY_TAGS),
            None,
        )
        if geometry_element is None:
            unsupported = next(
                (
                    child
                    for child in placemark.iter()
                    if _local(child.tag) in UNSUPPORTED_GEOMETRY_TAGS
                ),
                None,
            )
            if unsupported is not None:
                error = (
                    f"Placemark uses {_local(unsupported.tag)!r}, which has no measurable geometry."
                )
            else:
                error = "Placemark has no geometry element."
        else:
            try:
                geometry = _build_geometry(geometry_element)
            except ValueError as exc:
                error = str(exc)
            except Exception as exc:  # shapely rejects degenerate rings etc.
                error = f"Geometry could not be built: {exc}"

        features.append(
            RawFeature(
                index=index,
                geometry=geometry,
                crs=WGS84,
                properties=properties,
                feature_id=placemark.get("id") or properties.get("name"),
                layer=layer,
                warnings=feature_warnings,
                error=error,
            )
        )

    return ReadResult(
        features=features,
        crs=WGS84,
        crs_source=CRS_SOURCE,
        layers=layers,
        warnings=warnings,
    )


def read_kmz(payload: bytes, *, filename: str | None = None, max_bytes: int) -> ReadResult:
    """Read the KML document out of a KMZ (zipped KML) upload."""
    names = list_archive_names(payload)
    kml_names = [name for name in names if name.lower().endswith(".kml")]
    if not kml_names:
        raise InvalidGeospatialFileError(
            "KMZ archive contains no .kml document.", details={"members": names[:20]}
        )
    # The specification names doc.kml as the entry point; honour it when present.
    preferred = next(
        (name for name in kml_names if name.lower().split("/")[-1] == "doc.kml"),
        kml_names[0],
    )
    inner = read_archive_member(payload, preferred, max_bytes=max_bytes)
    result = read_kml(inner, filename=preferred)
    if len(kml_names) > 1:
        result.warnings.append(f"KMZ contains {len(kml_names)} KML documents; read {preferred!r}.")
    return result
