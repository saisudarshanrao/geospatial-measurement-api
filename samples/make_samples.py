"""Write the sample files to disk so they can be used with curl or Swagger UI.

python -m samples.make_samples            # writes into ./samples/
python -m samples.make_samples --out /tmp
"""

from __future__ import annotations

import argparse
from pathlib import Path

from samples import builders

FILES: dict[str, callable] = {
    "parcels_4326.zip": builders.sample_polygon_shapefile_4326,
    "square_utm_32643.zip": builders.sample_polygon_shapefile_utm,
    "lines_4326.zip": builders.sample_line_shapefile_4326,
    "cities_no_prj.zip": builders.sample_point_shapefile_no_prj,
    "survey.kml": builders.sample_kml,
    "survey.kmz": builders.sample_kmz,
    "wide_extent.kml": builders.sample_kml_large_extent,
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parent,
        help="Directory to write the sample files into.",
    )
    arguments = parser.parse_args()
    arguments.out.mkdir(parents=True, exist_ok=True)

    for name, build in FILES.items():
        target = arguments.out / name
        target.write_bytes(build())
        print(f"wrote {target} ({target.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
