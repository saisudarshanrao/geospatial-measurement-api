# Geospatial File Measurement API

Upload a Shapefile or a KML file; get back every feature it contains, with the
area of each polygon and the length of each line — measured in a coordinate
system where those numbers actually mean something.

Built with **FastAPI**. No GDAL required: `pip install -r requirements.txt` is
the whole setup.

```bash
curl -X POST http://127.0.0.1:8000/api/files/ -F "file=@samples/parcels_4326.zip"
```

```json
{
  "id": "4f1c0a9d2b77",
  "filename": "parcels_4326.zip",
  "format": "SHAPEFILE",
  "feature_count": 2,
  "crs": "EPSG:4326",
  "status": "COMPLETED",
  "summary": { "total_area_sq_m": 2403470.614, "total_area_hectares": 240.3470614 }
}
```

---

## Contents

- [The problem this solves](#the-problem-this-solves)
- [Setup](#setup)
- [API](#api)
- [Architecture](#architecture)
- [CRS handling](#crs-handling)
- [Design decisions](#design-decisions)
- [Testing](#testing)
- [Known limitations](#known-limitations)
- [Learning](#learning)
- [Future scope](#future-scope)

---

## The problem this solves

A KML file stores longitude and latitude in **degrees**. Hand those coordinates
to a planar geometry library and ask for an area, and you get a number back — it
just isn't an area:

```python
>>> from shapely.geometry import Polygon
>>> parcel = Polygon([(77.59, 12.97), (77.60, 12.97), (77.60, 12.98), (77.59, 12.98)])
>>> parcel.area
9.999999999976694e-05      # "square degrees" — meaningless
```

A degree of latitude is about 111 km everywhere; a degree of longitude is 111 km
at the equator and 0 km at the pole. So "square degrees" cannot be converted to
square metres by any single constant.

This service reprojects each feature into a coordinate system measured in metres
before it measures anything:

```console
$ curl -s "http://127.0.0.1:8000/api/files/$ID/measurements/" | jq '.measurements[0].measurement'
{
  "kind": "AREA",
  "measurable": true,
  "measurement_crs": "EPSG:32643",
  "measurement_crs_name": "WGS 84 / UTM zone 43N",
  "method": "utm",
  "area_sq_m": 1201683.919,
  "area_hectares": 120.1683919,
  "perimeter_m": 4385.062,
  "note": "Reprojected to EPSG:32643, the UTM zone containing this feature."
}
```

Every measurement says which CRS produced it and why that CRS was chosen, so the
number is auditable rather than merely plausible.

---

## Setup

Requires **Python 3.12+** (the pinned `pyproj` requires 3.12). No system GDAL, PROJ or GEOS packages are needed —
`shapely`, `pyproj` and `lxml` ship binary wheels and `pyshp` is pure Python.

```bash
git clone https://github.com/saisudarshanrao/geospatial-measurement-api.git
cd geospatial-measurement-api

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt

uvicorn app.main:app --reload
```

The service is now on <http://127.0.0.1:8000>, with interactive docs at
**<http://127.0.0.1:8000/docs>**.

Generate the sample files to try it with:

```bash
python -m samples.make_samples        # writes into ./samples/
```

### With Make

```bash
make install    # create .venv and install dev dependencies
make run        # uvicorn with autoreload
make test       # pytest
make cover      # pytest with a coverage report
make lint       # ruff check + format --check
make samples    # write the sample files
```

### With Docker

```bash
docker compose up --build             # http://127.0.0.1:8000
```

### Configuration

Every setting is an environment variable; the defaults are fine for local use.

| Variable | Default | Purpose |
| --- | --- | --- |
| `GEOAPI_MAX_UPLOAD_BYTES` | `52428800` (50 MiB) | Largest accepted upload. |
| `GEOAPI_MAX_UNCOMPRESSED_BYTES` | `524288000` (500 MiB) | Zip-bomb ceiling on expanded size. |
| `GEOAPI_MAX_ARCHIVE_ENTRIES` | `2000` | Maximum members in an archive. |
| `GEOAPI_STORAGE_DIR` | `./var/uploads` | Where original uploads are kept. |
| `GEOAPI_KEEP_UPLOADS` | `true` | Set `false` to discard bytes after parsing. |
| `GEOAPI_DEFAULT_STRATEGY` | `auto` | `auto`, `utm` or `geodesic`. |
| `GEOAPI_DEFAULT_PAGE_SIZE` | `100` | Default `limit` on list endpoints. |
| `GEOAPI_MAX_PAGE_SIZE` | `1000` | Largest accepted `limit`. |
| `GEOAPI_CORS_ORIGINS` | `*` | Comma-separated allowed origins. |
| `GEOAPI_LOG_LEVEL` | `INFO` | Root log level. |

---

## API

Interactive documentation: `/docs` (Swagger UI) and `/redoc`. Machine-readable
schema: `/openapi.json`.

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/files/` | Upload and process a file |
| `GET` | `/api/files/` | List uploaded files, newest first |
| `GET` | `/api/files/{id}/` | Information about one file |
| `GET` | `/api/files/{id}/measurements/` | Per-feature measurements |
| `GET` | `/api/files/{id}/features/` | Full feature records (geometry, CRS, properties) |
| `GET` | `/api/files/{id}/geojson/` | Features as a GeoJSON `FeatureCollection` |
| `DELETE` | `/api/files/{id}/` | Delete a file and its stored bytes |
| `GET` | `/health` | Liveness, plus what the service supports |

Accepted uploads: **`.zip` containing a Shapefile**, **`.kml`**, and
**`.kmz`** (zipped KML) as a bonus.

### `POST /api/files/`

Multipart upload under the field name `file`. Optional query parameter
`strategy` (`auto` | `utm` | `geodesic`, default `auto`).

```bash
curl -X POST http://127.0.0.1:8000/api/files/ \
  -F "file=@samples/survey.kml"
```

`201 Created` — the body is identical to `GET /api/files/{id}/`, so a client
that only wants the feature count does not need a second request:

```json
{
  "id": "d3c853d1eb39",
  "filename": "survey.kml",
  "size_bytes": 1532,
  "format": "KML",
  "feature_count": 6,
  "crs": "EPSG:4326",
  "crs_name": "WGS 84",
  "crs_source": "kml_specification",
  "measurement_strategy": "auto",
  "status": "COMPLETED_WITH_ERRORS",
  "layers": ["Sample survey", "Sample survey/Mixed"],
  "created_at": "2026-10-07T10:47:45.898081Z",
  "processed_at": "2026-10-07T10:47:46.147391Z",
  "processing_time_ms": 249.006,
  "warnings": [],
  "error": null,
  "summary": {
    "feature_count": 6,
    "measurable_count": 3,
    "unsupported_count": 0,
    "error_count": 2,
    "geometry_type_counts": {
      "GeometryCollection": 1, "LineString": 1, "NONE": 2, "Point": 1, "Polygon": 1
    },
    "total_area_sq_m": 2355834.75,
    "total_area_sq_km": 2.35583475,
    "total_area_hectares": 235.583475,
    "total_length_m": 4186.811,
    "total_length_km": 4.186811
  }
}
```

**`status`** is one of:

| Status | Meaning |
| --- | --- |
| `COMPLETED` | Every feature was read and measured. |
| `COMPLETED_WITH_ERRORS` | The file was read; some features could not be. |
| `PENDING`, `PROCESSING`, `FAILED` | Reserved for asynchronous processing (see [Future scope](#future-scope)). |

### `GET /api/files/{id}/measurements/`

Query parameters: `strategy`, `limit`, `offset`, `include_geometry`,
`include_properties`.

Omit `strategy` and you get the measurements exactly as the file was processed.
Supplying a *different* value recomputes them from the retained geometries — no
re-upload needed, which makes it cheap to compare the projected answer with the
ellipsoidal one.

```bash
curl "http://127.0.0.1:8000/api/files/$ID/measurements/?limit=2"
```

```json
{
  "file_id": "d3c853d1eb39",
  "filename": "survey.kml",
  "status": "COMPLETED_WITH_ERRORS",
  "crs": "EPSG:4326",
  "measurement_strategy": "auto",
  "units": { "area": "square_metre", "length": "metre" },
  "summary": { "feature_count": 6, "measurable_count": 3, "error_count": 2 },
  "page": { "total": 6, "count": 2, "limit": 2, "offset": 0 },
  "measurements": [
    {
      "feature_index": 0,
      "feature_id": "block-a",
      "geometry_type": "Polygon",
      "source_crs": "EPSG:4326",
      "layer": "Sample survey",
      "measurement": {
        "kind": "AREA",
        "measurable": true,
        "measurement_crs": "EPSG:32643",
        "measurement_crs_name": "WGS 84 / UTM zone 43N",
        "method": "utm",
        "area_sq_m": 1153616.446,
        "area_sq_km": 1.153616446,
        "area_hectares": 115.3616446,
        "perimeter_m": 5262.075,
        "length_m": null,
        "length_km": null,
        "reason": null,
        "note": "Reprojected to EPSG:32643, the UTM zone containing this feature."
      },
      "warnings": [],
      "error": null
    },
    {
      "feature_index": 1,
      "feature_id": "Access road",
      "geometry_type": "LineString",
      "source_crs": "EPSG:4326",
      "layer": "Sample survey",
      "measurement": {
        "kind": "LENGTH",
        "measurable": true,
        "measurement_crs": "EPSG:32643",
        "method": "utm",
        "length_m": 2636.019,
        "length_km": 2.636019,
        "area_sq_m": null
      },
      "warnings": [],
      "error": null
    }
  ]
}
```

What gets measured:

| Geometry | `kind` | Reported |
| --- | --- | --- |
| `Polygon`, `MultiPolygon` | `AREA` | `area_sq_m`, `area_sq_km`, `area_hectares`, `perimeter_m` |
| `LineString`, `MultiLineString`, `LinearRing` | `LENGTH` | `length_m`, `length_km` |
| `Point`, `MultiPoint` | `NONE` | nothing — `measurable: true`, with a `reason` |
| `GeometryCollection` | `MIXED` | area and length of its respective parts |
| anything else, or no geometry | `UNSUPPORTED` | nothing — `measurable: false`, with a `reason` |

`reason` says why there is no number; `note` says how the CRS was chosen.
A `note` is informational — a successful measurement never reports a warning
just because it had to reproject.

### `GET /api/files/{id}/features/`

The full record for each feature, which is what the processing stage extracts:
**index**, **id**, **geometry type**, **geometry** (GeoJSON), **CRS**,
**properties**, and the measurement. Pass `include_geometry=false` to trim the
payload.

```json
{
  "file_id": "d3c853d1eb39",
  "page": { "total": 6, "count": 1, "limit": 1, "offset": 0 },
  "features": [
    {
      "feature_index": 0,
      "feature_id": "block-a",
      "geometry_type": "Polygon",
      "source_crs": "EPSG:4326",
      "layer": "Sample survey",
      "geometry": { "type": "Polygon", "coordinates": [[[77.59, 12.97], "..."]] },
      "properties": { "name": "Block A", "owner": "City", "survey_no": "12/3" },
      "measurement": { "kind": "AREA", "area_sq_m": 1153616.446 },
      "warnings": [],
      "error": null
    }
  ]
}
```

### Errors

Every error uses one envelope, so a client has one shape to parse:

```json
{
  "error": {
    "code": "unsupported_file_type",
    "message": "Unsupported file type. Accepted uploads are a .zip containing a shapefile, a .kml document, or a .kmz archive.",
    "details": { "accepted_extensions": [".zip", ".kml", ".kmz"] }
  }
}
```

| Status | `code` | When |
| --- | --- | --- |
| 400 | `unsupported_file_type` | Not a Shapefile zip, KML or KMZ |
| 400 | `empty_upload` | Zero-byte upload |
| 400 | `invalid_parameter` | Unknown `strategy`, `limit` above the maximum |
| 404 | `not_found` | No such file id |
| 413 | `file_too_large` | Over `GEOAPI_MAX_UPLOAD_BYTES` |
| 422 | `invalid_geospatial_file` | Right format, unreadable content |
| 422 | `validation_error` | Request failed schema validation |
| 500 | `internal_error` | Unexpected; includes the `request_id` to grep logs with |

A *file* that cannot be read is an error. A *feature* that cannot be read is
not: it comes back with its own `error` string while every other feature is
measured, and the file's status becomes `COMPLETED_WITH_ERRORS`. One bad record
in a 10,000-feature survey should not cost you the other 9,999.

Every response carries an `X-Request-ID` header (echoed if you send one), and
that id appears in the structured JSON logs.

---

## Architecture

### Application structure

```
app/
├── main.py                 FastAPI factory, middleware, exception handlers
├── config.py               Settings from environment variables
├── models.py               Domain dataclasses + the status/kind enums
├── schemas.py              Pydantic response models + domain → wire mapping
├── errors.py               Domain exceptions carrying their HTTP status
├── logging_config.py       Single-line JSON logs to stdout
├── api/
│   ├── deps.py             Dependencies: repository, processor, pagination, strategy
│   └── routes_files.py     The /api/files endpoints
├── geo/                    ← no FastAPI imports anywhere below this line
│   ├── detect.py           Format sniffing (content first, extension second)
│   ├── archive.py          Hardened zip extraction
│   ├── crs.py              CRS resolution and measurement-CRS selection
│   ├── measure.py          Per-geometry measurement
│   └── readers/
│       ├── base.py         RawFeature / ReadResult + attribute normalisation
│       ├── shapefile_reader.py
│       └── kml_reader.py
└── services/
    ├── processing.py       The pipeline that ties it together
    └── storage.py          Repository + upload store (both behind Protocols)
```

The dependency rule is one-directional: `api` → `services` → `geo`. Nothing in
`geo/` imports FastAPI, which is why the measurement logic can be unit-tested
without an HTTP client and could be lifted into a CLI or a batch worker
unchanged.

### File-processing flow

```
POST /api/files/
   │
   ├─ 1. read upload in 64 KiB chunks, aborting past the size limit
   │        (Content-Length is a claim, not a guarantee → 413)
   │
   ├─ 2. detect format from magic bytes, not the filename
   │        PK\x03\x04 + a .shp member  → SHAPEFILE
   │        PK\x03\x04 + a .kml member  → KMZ
   │        <kml …>                     → KML
   │        otherwise                   → 400 with an explanation
   │
   ├─ 3. read features  (run in a worker thread: CPU-bound)
   │        SHAPEFILE → extract archive to a temp dir (traversal, bomb and
   │                    entry-count checks), group members by stem, read each
   │                    layer with pyshp, CRS from its .prj
   │        KML/KMZ   → parse with lxml (external entities disabled),
   │                    match elements by local name, CRS is EPSG:4326 by spec
   │
   ├─ 4. measure each feature independently
   │        choose a measurement CRS → reproject → compute → record how
   │        a per-feature failure is captured, never raised
   │
   ├─ 5. aggregate the file-level summary and set the status
   │
   └─ 6. store the record; respond 201 with the file information
```

Parsing and measurement run in a thread (`run_in_threadpool`) so a large file
cannot block the event loop while other requests are served.

### Measurement calculation flow

For each feature, in `app/geo/measure.py`:

1. **Guard** — no geometry, no CRS, or an empty geometry is reported as
   unmeasurable rather than raised.
2. **Classify** by geometry family: polygonal, linear, puntal, collection.
   Points are *supported*; they simply have no size. Dispatching on the family
   rather than the exact type name means the multi-part variants need no extra
   code.
3. **Flatten to 2D.** A Z ordinate is dropped with a warning, so a 3D KML line
   is never measured as a slope distance in one path and a plan distance in
   another.
4. **Repair** an invalid polygon with `make_valid` and say so in a warning —
   self-intersecting rings are common in hand-digitised data, and their raw
   `.area` is wrong rather than merely imprecise.
5. **Plan the CRS** (below) — polygons ask for an equal-area fallback, lines for
   an equidistant one.
6. **Reproject and compute.** `shapely.transform` sends all vertices to PROJ in
   one vectorised call. Area and length are scaled by the CRS's
   unit-to-metre factor, so a file in US survey feet still reports SI units.
7. **Report** the number together with the CRS used, the method that chose it,
   and any warnings.

---

## CRS handling

The rule: **never compute area or length from degrees.** Four strategies
implement it, and the response always names the one that was used.

| `method` | Chosen when | Accuracy |
| --- | --- | --- |
| `source_crs` | The file is already projected | Exact for its CRS; no reprojection error added |
| `utm` | Geographic input fitting one UTM zone | ~0.04% in distance, ~0.1% in area at the zone edge |
| `azimuthal` | Geographic input too wide for one zone, or beyond ±84° | Equal-area (`laea`) for areas, equidistant (`aeqd`) for lengths |
| `geodesic` | Requested with `?strategy=geodesic` | No projection distortion at all |

**Projected input is measured in place.** Reprojecting a file that is already in
metres would only add error. The axis unit is read from the CRS and converted,
so EPSG:2229 (US survey feet) reports square metres correctly: a 1000 ft square
comes back as 92,903 m², not 1,000,000.

**The UTM zone is chosen per feature, not per file**, from that feature's own
centroid. A national dataset spanning four zones gets a locally accurate
projection for each feature instead of one compromise for all of them. Zone
lookup goes through `pyproj.database.query_utm_crs_info`, so it is PROJ's
answer, not arithmetic on longitude.

**The azimuthal fallback** handles what UTM cannot: features wider than 12° of
longitude, or outside ±80–84° of latitude. The projection is centred on the
feature, and the *kind* depends on what is being measured — Lambert azimuthal
equal-area for area, azimuthal equidistant for length. Each is exact for its own
quantity at the centre of projection.

**Antimeridian features** are detected (a bounding box wider than 180° is
treated as a dateline crossing, not a global feature) and their longitudes
unwrapped, so a small polygon at 180° gets a projection centred on the
antimeridian rather than one centred on Greenwich.

**Geodesic measurement** (`?strategy=geodesic`) integrates on the WGS 84
ellipsoid via `pyproj.Geod`, with no projection at all. It is the most accurate
option and makes a good cross-check — for a typical urban parcel the default
projected answer lands within 0.12% of it:

```console
auto      area=   1201683.919  crs=EPSG:32643  method=utm
geodesic  area=   1200289.843  crs=EPSG:4326   method=geodesic
```

It is not the default because almost every GIS tool reports the projected
figure, and a service whose numbers do not match QGIS is a service people
distrust. Both are one request apart, so you can have either.

CRS provenance is reported too, via `crs_source`:

| `crs_source` | Meaning |
| --- | --- |
| `prj` | Read from the Shapefile's `.prj` |
| `kml_specification` | KML is EPSG:4326 by definition — not a guess |
| `assumed_default` | No CRS found; EPSG:4326 assumed, with a warning |

---

## Design decisions

### FastAPI over Django REST Framework

The service has no relational model, no admin, no authentication and no
migrations. DRF's value is in what it gives a database-backed resource API, and
none of that applies here. FastAPI contributes the two things this project does
need: generated OpenAPI documentation (so `/docs` is a working client, not a
written document) and Pydantic validation at the boundary.

### No GDAL, GeoPandas or Fiona

The obvious stack for this task is GeoPandas + Fiona, where
`geopandas.read_file()` reads both formats in one line. I chose
**shapely + pyproj + pyshp + lxml** instead:

- **Installability.** Fiona needs GDAL. Wheels have improved, but GDAL remains
  the dependency most likely to turn "clone and run" into an afternoon of
  system packages. Every dependency here is a wheel on Linux, macOS and
  Windows, so the setup instructions are one `pip install` and the Dockerfile
  needs no `apt-get`.
- **KML support is not actually free.** Fiona reads KML through GDAL's LIBKML
  or KML driver, and whether those are compiled in varies by build. A GDAL that
  installs fine can still refuse the KML half of the requirement.
- **Weight.** GeoPandas pulls in pandas for what is a streaming parse; the
  service never needs a dataframe.
- **Control over failure.** Reading records myself is what makes
  "one bad feature does not fail the file" implementable: GeoPandas would
  surface a corrupt record as an exception over the whole read.

The cost is real and worth naming: I hand-wrote a KML parser, so exotic KML
(`NetworkLink`, `Region`, time primitives) is not covered, whereas LIBKML would
have handled more. For the formats in scope this trade favoured deployability.
A `Reader` protocol sits behind both readers, so adding a Fiona-backed reader
for GeoPackage or GeoJSON is additive rather than a rewrite.

### Per-feature CRS selection, not per-file

The simpler design picks one projection for the whole file from its overall
extent. For a file spanning several UTM zones that means measuring most features
in a projection chosen for somewhere else. Per-feature selection costs a cached
transformer lookup (`functools.lru_cache` on the CRS pair, so the PROJ pipeline
is built once per distinct pair) and is materially more accurate. The extent-based
fallback still exists — it just applies to the feature, not the file.

### Synchronous processing

Measurement is arithmetic. For files within the configured limit it finishes
inside the request, so a queue would add a round trip, a broker and a new class
of failure without improving the response. The `status` field is nevertheless a
full state machine and the pipeline is a single call, so moving steps 3–5 onto a
worker is a change in one module rather than an API break.

### In-memory repository behind a Protocol

There is nothing to persist relationally: the measurements are a pure function
of the uploaded bytes. An in-memory store keeps the service stateless-by-default
and the tests fast. Both the repository and the upload store are `Protocol`s, so
substituting PostgreSQL means implementing four methods — no router changes.
The trade-off is explicit: **records are lost on restart, and a multi-worker
deployment gives each worker its own view.** For a measurement API that is
acceptable; see [Future scope](#future-scope) for when it stops being.

### Geometries are retained in the record

Keeping the parsed shapely geometries in memory costs RAM but buys
`?strategy=geodesic` as a re-read of an existing file rather than a re-upload,
which is what makes cross-checking a measurement cheap.

### Uploads treated as hostile input

A geospatial file is a user-supplied archive and a user-supplied XML document,
which is to say two well-known attack surfaces:

- **Zip extraction** rejects path traversal (`../`), absolute and drive-letter
  member paths, archives whose declared *or actual* expanded size exceeds the
  limit (the header is a claim, so expansion is re-checked while writing), and
  archives with an absurd member count.
- **XML parsing** disables external entity resolution, network access and DTD
  loading — otherwise a KML upload could read `/etc/passwd` via XXE or exhaust
  memory through entity expansion. Both are covered by tests.
- **Upload size** is enforced while streaming, not from `Content-Length`.
- Stored filenames are sanitised, and each upload gets its own directory, so a
  crafted name cannot collide with or overwrite another.

### Measured values are rounded

Linear values to the millimetre, areas to the square millimetre. Beyond that the
digits are float noise, and echoing them implies precision the input does not
have.

---

## Testing

116 tests, 91% statement coverage. `pytest` runs with `-W error`, so a new
warning fails the build.

```bash
make test        # or: pytest
make cover       # coverage report
```

| File | Covers |
| --- | --- |
| `test_crs.py` | Strategy selection, UTM per feature, unit factors, antimeridian, round-tripping |
| `test_measure.py` | Known areas, the degrees trap, holes, multi-parts, repair, 3D, projected-vs-geodesic agreement |
| `test_kml_reader.py` | Namespace variants, geometry kinds, ExtendedData, XXE, billion laughs, recovery mode |
| `test_shapefile_reader.py` | `.prj`/`.cpg` handling, NULL shapes, multiple layers, zip-bomb and traversal defences |
| `test_detect.py` | Content-first detection, misleading extensions, helpful rejections |
| `test_api.py` | Every endpoint, pagination, strategy switching, and each error status |

Sample files are **generated in code** (`samples/builders.py`) rather than
committed as binaries, so a reviewer can read exactly what geometry each test
asserts against. Fixtures that pin real numbers:

- a 250 m square in EPSG:32643 must measure **62,500 m²** exactly;
- a 1000 ft square in EPSG:2229 must measure **92,903 m²**, not 1,000,000;
- the projected and geodesic answers for an urban parcel must agree within 0.5%.

CI runs lint, the suite on Python 3.12/3.13, and a smoke test that starts
the real server and uploads a real file.

---

## Known limitations

Stated plainly, because knowing where a tool stops is part of trusting it.

- **Records do not survive a restart**, and each worker process has its own
  store. Run a single worker, or add the database described below.
- **Measurements are 2D.** Z ordinates are dropped (with a warning). No slope
  distance or surface area.
- **Long edges are ambiguous, and the strategies disagree about them.** A
  polygon whose four vertices span 80° of longitude does not define one region:
  the planar strategies join vertices with straight lines in coordinate space,
  while `geodesic` follows great circles, which bow polewards. For an 80°×20°
  box the two answers are 20.3 and 23.4 million km² — a 13% gap, not because
  either is wrong, but because the source data is underspecified. Densify the
  edges and they converge to within 0.01%. A test pins both behaviours so
  neither can drift silently.
- **A zip with several Shapefiles** is read in full, with features tagged by
  layer, but the file-level `crs` reports the first layer's. Per-feature
  `source_crs` is always correct.
- **Hand-written KML parser**: `Placemark` geometries, `MultiGeometry`,
  `gx:Track`, `ExtendedData` and folder paths are supported;
  `NetworkLink`, `Region` and time primitives are not.
- **Whole uploads are read into memory**, bounded by the size limit. Streaming
  would be needed for files much larger than that.
- **No authentication or rate limiting.** Both belong at the gateway, and
  neither was in scope.

---

## Learning

Things this exercise actually taught me, as opposed to things I already knew and
re-typed.

**"Reproject before measuring" is the beginning of the problem, not the end.**
I expected the CRS work to be one `Transformer` call. The real question is
*which* projection, and it has no single answer: a UTM zone is excellent for a
city block and undefined for a polygon spanning a continent. Writing the
fallback chain — projected source, then UTM, then azimuthal, with the azimuthal
*kind* depending on whether an area or a length is wanted — was where most of
the thinking went.

**The biggest error source was not the projection.** I wrote a test asserting
that the equal-area fallback agrees with the geodesic computation for a large
polygon. It failed by 13% — 20.3 against 23.4 million km² — which looked like a
bug in my projection choice. It was not: the two methods were integrating
different boundaries, because four vertices spanning 80° of longitude do not say
whether the edges between them are straight in coordinate space or follow great
circles. Densifying the edges collapsed the disagreement to under 0.01%. The
lesson is that beyond a certain feature size the input data, not the arithmetic,
is the limiting factor — so I documented it and pinned both behaviours in tests
rather than picking a winner quietly.

**UTM is conformal, not equal-area.** A test comparing projected and geodesic
areas failed at a 0.1% tolerance. That was not a bug either: it is the scale
distortion you accept 2.6° away from a zone's central meridian. Finding out
*why* a tolerance is too tight turned out to be more valuable than loosening it.

**Dependency choice is a design decision with user-visible consequences.**
`geopandas.read_file()` would have replaced both readers with one line. Choosing
against it meant writing a KML parser, and it bought a project that installs
with one `pip install` on any platform and a Dockerfile with no `apt-get`. I had
previously filed "which library" under convenience; here it determined whether
the setup instructions were three lines or a troubleshooting section.

**Error semantics deserve the same care as the happy path.** The design question
that took longest was not how to compute an area but what a Point *is*. It is
not an error and not a failure — it is a valid feature with no size, and
flattening that into `null` or a 400 would lose information the caller needs.
That is why `measurable`, `reason` and `note` are three separate fields: "there
is no number", "here is why", and "here is how the number was produced" are
genuinely different statements.

**Tests surfaced three real bugs that review had not.** A `GET` with no
`strategy` silently re-measured with the server default instead of returning the
file's own measurements; a rejected upload left an unreachable record in the
repository; and `pyproj`'s `to_proj4()` raised a `UserWarning` on every
azimuthal measurement, which `-W error` turned into a hard failure. All three
were found by writing the assertion first and watching it fail for a reason I
had not predicted.

---

## Future scope

Roughly in the order I would actually do it.

1. **Persistence.** A `FileRepository` implementation over PostgreSQL —
   PostGIS if geometry queries ever matter — so records survive restarts and
   multiple workers share one view. The `Protocol` is already in place.
2. **Asynchronous processing for large files.** Keep the synchronous path for
   small uploads; above a threshold return `202 Accepted` with status
   `PENDING`, process on an `arq`/Celery worker, and let clients poll
   `GET /api/files/{id}/`. The status vocabulary already covers this.
3. **More formats.** GeoJSON, GeoPackage, GML and DXF, each as a new `Reader`
   behind the existing protocol. This is where an *optional* Fiona-backed
   reader would earn its dependency, selected at runtime if GDAL is present.
4. **Richer measurements.** Centroids, bounding boxes, convex hulls, geodesic
   azimuths, and 3D/slope length where Z is present.
5. **Explicit CRS overrides.** `?assume_crs=EPSG:27700` for files whose `.prj`
   is missing or wrong, and `?target_crs=` to force a measurement CRS.
6. **Edge densification.** A `?densify=0.1` parameter that segmentises long
   edges before measuring, making the continental-feature ambiguity above
   something a caller can resolve rather than merely read about.
7. **Streaming large uploads** straight to disk, so the memory ceiling stops
   being the upload limit.
8. **Operational hardening.** Authentication, per-key rate limiting,
   Prometheus metrics, OpenTelemetry traces, and a retention job that expires
   stored uploads.
9. **A small web client.** Drag in a file, see the features on a Leaflet map
   coloured by area, click one for its measurement. The `/geojson/` endpoint
   exists to make this straightforward.

---

## License

[MIT](LICENSE).
