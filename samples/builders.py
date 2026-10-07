"""Build sample Shapefile/KML/KMZ payloads in memory.

Having the fixtures generated rather than committed as binaries keeps the
repository readable: a reviewer can see exactly what geometry each test asserts
against, and the tests do not depend on an opaque blob.
"""

from __future__ import annotations

import zipfile
from collections.abc import Iterable, Sequence
from io import BytesIO
from typing import Any

import shapefile  # pyshp

Coordinate = tuple[float, float]
Ring = Sequence[Coordinate]

WGS84_WKT = (
    'GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,298.257223563]],'
    'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]]'
)
UTM43N_WKT = (
    'PROJCS["WGS_1984_UTM_Zone_43N",GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",'
    'SPHEROID["WGS_1984",6378137.0,298.257223563]],PRIMEM["Greenwich",0.0],'
    'UNIT["Degree",0.0174532925199433]],PROJECTION["Transverse_Mercator"],'
    'PARAMETER["False_Easting",500000.0],PARAMETER["False_Northing",0.0],'
    'PARAMETER["Central_Meridian",75.0],PARAMETER["Scale_Factor",0.9996],'
    'PARAMETER["Latitude_Of_Origin",0.0],UNIT["Meter",1.0]]'
)


# ----------------------------------------------------------------- shapefiles


def build_shapefile_zip(
    *,
    shape_type: int,
    fields: Sequence[tuple[str, str, int]],
    records: Iterable[tuple[Sequence[Any], Any]],
    prj_wkt: str | None = WGS84_WKT,
    layer_name: str = "layer",
    extra_members: dict[str, bytes] | None = None,
    omit: Sequence[str] = (),
) -> bytes:
    """Zip up a shapefile built from ``records``.

    ``records`` yields ``(geometry, attributes)`` pairs, where geometry is a
    list of rings for polygons, a list of parts for polylines, or an ``(x, y)``
    pair for points. ``omit`` drops components so missing-sidecar handling can
    be exercised.
    """
    shp, shx, dbf = BytesIO(), BytesIO(), BytesIO()
    writer = shapefile.Writer(shp=shp, shx=shx, dbf=dbf, shapeType=shape_type)
    for name, field_type, size in fields:
        writer.field(name, field_type, size)

    for geometry, attributes in records:
        if geometry is None:
            writer.null()
        elif shape_type == shapefile.POLYGON:
            writer.poly(geometry)
        elif shape_type == shapefile.POLYLINE:
            writer.line(geometry)
        elif shape_type == shapefile.POINT:
            writer.point(*geometry)
        else:  # pragma: no cover - only the three types above are generated
            raise ValueError(f"Unsupported shape type {shape_type}")
        writer.record(*attributes)
    writer.close()

    members = {
        f"{layer_name}.shp": shp.getvalue(),
        f"{layer_name}.shx": shx.getvalue(),
        f"{layer_name}.dbf": dbf.getvalue(),
    }
    if prj_wkt is not None:
        members[f"{layer_name}.prj"] = prj_wkt.encode("utf-8")
    for extension in omit:
        members.pop(f"{layer_name}{extension}", None)
    if extra_members:
        members.update(extra_members)

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def square_ring(west: float, south: float, width: float, height: float) -> list[Coordinate]:
    """A closed, clockwise ring - the winding order shapefiles use for shells."""
    return [
        (west, south),
        (west, south + height),
        (west + width, south + height),
        (west + width, south),
        (west, south),
    ]


def sample_polygon_shapefile_4326() -> bytes:
    """Two small parcels near Bengaluru, in geographic coordinates."""
    return build_shapefile_zip(
        shape_type=shapefile.POLYGON,
        fields=[("name", "C", 40), ("parcel_id", "N", 10)],
        records=[
            ([square_ring(77.5900, 12.9700, 0.0100, 0.0100)], ["North block", 1]),
            ([square_ring(77.6100, 12.9500, 0.0050, 0.0200)], ["South block", 2]),
        ],
        prj_wkt=WGS84_WKT,
        layer_name="parcels",
    )


def sample_polygon_shapefile_utm(side_metres: float = 250.0) -> bytes:
    """One square of an exactly known size, in a projected CRS (EPSG:32643)."""
    east, north = 500_000.0, 1_400_000.0
    return build_shapefile_zip(
        shape_type=shapefile.POLYGON,
        fields=[("name", "C", 40)],
        records=[([square_ring(east, north, side_metres, side_metres)], ["Test square"])],
        prj_wkt=UTM43N_WKT,
        layer_name="square_utm",
    )


def sample_line_shapefile_4326() -> bytes:
    """A single polyline running east along the equator."""
    return build_shapefile_zip(
        shape_type=shapefile.POLYLINE,
        fields=[("name", "C", 40)],
        records=[([[(77.0, 0.0), (77.1, 0.0)]], ["Equator segment"])],
        prj_wkt=WGS84_WKT,
        layer_name="lines",
    )


def sample_point_shapefile_no_prj() -> bytes:
    """Points with no ``.prj``, so the assumed-CRS path is exercised."""
    return build_shapefile_zip(
        shape_type=shapefile.POINT,
        fields=[("name", "C", 40)],
        records=[((77.5946, 12.9716), ["Bengaluru"]), ((72.8777, 19.0760), ["Mumbai"])],
        prj_wkt=None,
        layer_name="cities",
    )


# ------------------------------------------------------------------------ KML

_KML_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>{document_name}</name>
{body}
  </Document>
</kml>
"""


def kml_document(body: str, *, document_name: str = "Sample survey") -> bytes:
    return _KML_TEMPLATE.format(document_name=document_name, body=body).encode("utf-8")


def polygon_placemark(
    name: str, shell: Ring, holes: Sequence[Ring] = (), *, extended: dict[str, str] | None = None
) -> str:
    def ring(coordinates: Ring) -> str:
        return " ".join(f"{lon},{lat}" for lon, lat in coordinates)

    inner = "".join(
        f"<innerBoundaryIs><LinearRing><coordinates>{ring(hole)}</coordinates>"
        "</LinearRing></innerBoundaryIs>"
        for hole in holes
    )
    extended_xml = ""
    if extended:
        data = "".join(
            f'<Data name="{key}"><value>{value}</value></Data>' for key, value in extended.items()
        )
        extended_xml = f"<ExtendedData>{data}</ExtendedData>"
    return (
        f'    <Placemark id="{name.lower().replace(" ", "-")}">'
        f"<name>{name}</name>{extended_xml}"
        f"<Polygon><outerBoundaryIs><LinearRing><coordinates>{ring(shell)}</coordinates>"
        f"</LinearRing></outerBoundaryIs>{inner}</Polygon></Placemark>"
    )


def line_placemark(name: str, coordinates: Ring) -> str:
    joined = " ".join(f"{lon},{lat}" for lon, lat in coordinates)
    return (
        f"    <Placemark><name>{name}</name>"
        f"<LineString><coordinates>{joined}</coordinates></LineString></Placemark>"
    )


def point_placemark(name: str, coordinate: Coordinate) -> str:
    return (
        f"    <Placemark><name>{name}</name>"
        f"<Point><coordinates>{coordinate[0]},{coordinate[1]}</coordinates></Point></Placemark>"
    )


def sample_kml() -> bytes:
    """A document covering every branch worth testing at the API level.

    One polygon with a hole, one line, one point, a multi-geometry, a placemark
    with no geometry at all, and a ``Model`` placemark that has no measurable
    geometry.
    """
    shell = square_ring(77.5900, 12.9700, 0.0100, 0.0100)
    hole = square_ring(77.5930, 12.9730, 0.0020, 0.0020)
    body = "\n".join(
        [
            polygon_placemark(
                "Block A", shell, [hole], extended={"owner": "City", "survey_no": "12/3"}
            ),
            line_placemark("Access road", [(77.60, 12.97), (77.61, 12.98), (77.62, 12.98)]),
            point_placemark("Marker", (77.5946, 12.9716)),
            "    <Folder><name>Mixed</name>"
            "<Placemark><name>Combo</name><MultiGeometry>"
            "<Polygon><outerBoundaryIs><LinearRing><coordinates>"
            "77.70,12.90 77.70,12.91 77.71,12.91 77.71,12.90 77.70,12.90"
            "</coordinates></LinearRing></outerBoundaryIs></Polygon>"
            "<LineString><coordinates>77.72,12.90 77.73,12.91</coordinates></LineString>"
            "</MultiGeometry></Placemark></Folder>",
            "    <Placemark><name>No geometry here</name>"
            "<description>Metadata only</description></Placemark>",
            "    <Placemark><name>A 3D model</name><Model><Location>"
            "<longitude>77.5</longitude><latitude>12.9</latitude>"
            "</Location></Model></Placemark>",
        ]
    )
    return kml_document(body)


def sample_kmz() -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("doc.kml", sample_kml())
    return buffer.getvalue()


def sample_kml_large_extent() -> bytes:
    """A polygon far too wide for one UTM zone, to exercise the fallback."""
    body = polygon_placemark(
        "Continental sweep",
        [(-60.0, -10.0), (-60.0, 10.0), (20.0, 10.0), (20.0, -10.0), (-60.0, -10.0)],
    )
    return kml_document(body, document_name="Wide extent")
