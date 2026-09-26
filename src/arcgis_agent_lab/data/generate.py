"""Deterministic synthetic GIS scenario generator.

Why synthetic data (read this first -- it is the project's central argument)
--------------------------------------------------------------------------
Real cadastral data has **no ground truth**. Hand a real parcel layer and a real
farmland layer and there is still no way to *verify* whether a given parcel
overlaps farmland: the authoritative answer sits in someone else's database,
behind permissions, and is sometimes itself contested. Without ground truth
there is no evaluation -- only a demo.

Synthetic scenes invert that. The scene is constructed, so **every answer is
known in advance**, and boundary cases that never co-occur in real data can be
built deliberately:

    tangency          intersection area is exactly 0, yet intersects() is True
    full containment  intersection area equals the parcel area
    partial overlap   a genuine fraction
    disjoint-but-near a non-zero distance, small enough to be confused

The last one matters most: it is what separates "is there an overlap" from
"how much area overlaps" -- two questions a naive agent conflates.

Output format
-------------
Standard **GeoJSON** written with the stdlib ``json`` module, not Shapefile or
FileGDB: fiona / geopandas / rasterio all require GDAL, which needs a
pre-compiled wheel on Windows and would put a rebuild step in the way of anyone
reproducing this. GeoJSON needs nothing beyond the stdlib. Importing into a
geodatabase is left to the bridge's own ``import_from_geojson`` tool.

**The files are named ``.json``, not ``.geojson``, and that is deliberate.**
``arcpy.conversion.JSONToFeatures`` -- which the bridge's import tool calls --
parses a ``.geojson`` file as **polygons only**: a point or polyline layer is
silently produced as an empty feature class with the default geometry type, and
nothing raises. Renaming the identical bytes to ``.json`` makes all three
geometry types import correctly. Verified by controlled experiment:

    facilities.geojson  -> 0 features, shapeType=Polygon (default)
    facilities.json     -> 10 features, shapeType=Point
    roads.geojson       -> 0 features, shapeType=Polygon (default)
    roads.json          -> 6 features, shapeType=Polyline

Two of six layers were empty for an entire evaluation run because of this, and
the resulting failures were attributed to the model until the cause was traced.
GeoJSON *is* JSON, so the extension costs nothing but a less familiar name.

Determinism
-----------
A fixed seed drives ``numpy.random.default_rng``, and geometry is pure Shapely
(no floating-point drift between runs). The same seed therefore produces
byte-identical files; ``scenario.json`` records a SHA-256 per layer so an
evaluation report can pin the exact data it was measured against.

Coordinate reference
--------------------
Everything is **computed in metres** (EPSG:4547, CGCS2000 / 3-degree
Gauss-Kruger, anchored near Meizhou, Guangdong) -- geometry, distances, areas
and the whole truth table.

What gets **written** is EPSG:4326, because RFC 7946 defines GeoJSON as WGS 84
and removed the ``crs`` member. This distinction matters more than it looks.

Writing projected metre coordinates into GeoJSON (which this generator did at
first) makes ArcGIS read them as degrees. Two things then go wrong, and neither
raises an error:

* coordinates in the 0-1000 range are partly inside the valid degree domain, so
  the features import successfully but carry a **wrong CRS** -- the metadata says
  WGS 84 while the numbers are metres;
* anything outside that domain -- a longitude of 874, a latitude of 535 -- is
  **silently dropped**. Five of the six layers lost features that way, two of
  them entirely.

The fix is a two-stage pipeline rather than a format change:

    generate  ->  GeoJSON in EPSG:4326   (legal per the spec, nothing is lost)
    prepare   ->  import, then project to EPSG:4547   (metres, as the truth assumes)

That also turns ``project_features`` from a hypothetical exercise into a step
the data pipeline genuinely requires, which is a better trap than inventing one.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from collections.abc import Iterable, Sequence

import numpy as np
from pyproj import Transformer
from shapely.affinity import translate
from shapely.geometry import LineString, Point, Polygon, mapping
from shapely.ops import transform as shapely_transform

#: Scene anchor in EPSG:4547 metres. Chosen so the scene lands near Meizhou,
#: Guangdong (the region this project's domain background comes from).
ORIGIN_X: float = 700_000.0
ORIGIN_Y: float = 2_700_000.0

#: Layers whose true CRS the GeoJSON cannot express (see module docstring).
LAYER_CRS: dict[str, int] = {
    "parcels": 4547,
    "farmland": 4547,
    "buildings": 4547,
    "roads": 4547,
    "facilities": 4547,
    "parcels_geographic": 4326,
}

#: Indices of parcels reserved for deliberate boundary construction.
IDX_CONTAINED = 0
IDX_TANGENT = 1
IDX_PARTIAL = 2
IDX_DISJOINT_NEAR = 3
RESERVED_PARCEL_SLOTS = 4


@dataclass(frozen=True)
class ScenarioConfig:
    """Knobs for one reproducible scene."""

    seed: int = 42
    extent_m: float = 1000.0
    n_parcels: int = 30
    #: Farmland coverage decides how many parcel/farmland pairs actually
    #: intersect. With too little farmland, nearly every "does it overlap?"
    #: answer is trivially False and a model can score well by always saying
    #: no -- the task set becomes guessable. Coverage is therefore a
    #: first-class parameter, not an afterthought.
    n_farmland: int = 6
    farmland_r_min: float = 110.0
    farmland_r_max: float = 160.0
    n_buildings: int = 40
    n_facilities: int = 10
    n_roads: int = 6


# --------------------------------------------------------------------------- #
# Geometry primitives
# --------------------------------------------------------------------------- #


def _irregular_polygon(
    rng: np.random.Generator, cx: float, cy: float, r_min: float, r_max: float
) -> Polygon:
    """A convex-ish irregular polygon around (cx, cy).

    ``buffer(0)`` repairs any self-intersection introduced by the random radii,
    so callers never receive an invalid geometry.
    """
    n = int(rng.integers(5, 9))
    angles = np.sort(rng.uniform(0.0, 2.0 * math.pi, n))
    radii = rng.uniform(r_min, r_max, n)
    coords = [
        (cx + r * math.cos(a), cy + r * math.sin(a))
        for a, r in zip(angles, radii, strict=True)
    ]
    return Polygon(coords).buffer(0)


def _tangent_parcel(farm: Polygon, depth: float = 25.0) -> Polygon:
    """A parcel sharing exactly one edge with ``farm`` -- tangency, area 0.

    The shared edge means ``intersects()`` is True while
    ``intersection().area`` is exactly 0. That gap is the single most useful
    discriminator in the task set.
    """
    ring = list(farm.exterior.coords)
    best: Polygon | None = None
    for i in range(len(ring) - 1):
        (x1, y1), (x2, y2) = ring[i], ring[i + 1]
        dx, dy = x2 - x1, y2 - y1
        length = math.hypot(dx, dy)
        if length < 40.0:  # need a usable edge
            continue
        ux, uy = dx / length, dy / length
        nx, ny = -uy, ux  # a normal to the edge
        candidate = Polygon(
            [(x1, y1), (x2, y2), (x2 + nx * depth, y2 + ny * depth),
             (x1 + nx * depth, y1 + ny * depth)]
        ).buffer(0)
        # Keep the parcel strictly OUTSIDE the farm so area is exactly zero;
        # flip the normal if this side points inward.
        if farm.intersects(candidate) and farm.intersection(candidate).area > 1e-9:
            candidate = Polygon(
                [(x1, y1), (x2, y2), (x2 - nx * depth, y2 - ny * depth),
                 (x1 - nx * depth, y1 - ny * depth)]
            ).buffer(0)
        if not farm.intersection(candidate).area > 1e-9:
            best = candidate
            if length > 120.0:
                break
    if best is None:  # pragma: no cover - the random scenes always yield one
        raise RuntimeError("could not construct a tangent parcel")
    return best


def _contained_parcel(rng: np.random.Generator, farm: Polygon) -> Polygon:
    """A parcel fully inside ``farm`` (intersection area == parcel area)."""
    inner = farm.buffer(-40.0)
    if inner.is_empty:  # pragma: no cover - farms are >= 300 m across
        inner = farm.buffer(-15.0)
    pt = inner.representative_point()
    return _irregular_polygon(rng, pt.x, pt.y, 12.0, 22.0)


def _overlapping_parcel(rng: np.random.Generator, farm: Polygon) -> Polygon:
    """A parcel genuinely straddling the farm boundary (0 < area < parcel area)."""
    ring = list(farm.exterior.coords)
    (x1, y1), (x2, y2) = ring[1], ring[2]
    mx, my = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    return _irregular_polygon(rng, mx, my, 18.0, 30.0)


def _near_but_disjoint(rng: np.random.Generator, farm: Polygon) -> Polygon:
    """A parcel close to ``farm`` but with zero intersection.

    Places the parcel centre just outside the farm's bounds and retries until
    the gap is small (a few metres) yet non-zero.
    """
    minx, miny, maxx, maxy = farm.bounds
    for _ in range(200):
        offset = float(rng.uniform(35.0, 60.0))
        cx = maxx + offset
        cy = (miny + maxy) / 2.0 + float(rng.uniform(-30.0, 30.0))
        candidate = _irregular_polygon(rng, cx, cy, 12.0, 20.0)
        if candidate.intersects(farm):
            continue
        if candidate.distance(farm) <= 0.0:
            continue
        return candidate
    raise RuntimeError("could not construct a disjoint-but-near parcel")


# --------------------------------------------------------------------------- #
# Layer builders
# --------------------------------------------------------------------------- #


def _build_farmland(rng: np.random.Generator, cfg: ScenarioConfig) -> list[Polygon]:
    """Large, mutually disjoint farmland blocks covering a useful fraction."""
    blocks: list[Polygon] = []
    margin = cfg.farmland_r_max
    for _ in range(cfg.n_farmland):
        for _attempt in range(400):
            cx = float(rng.uniform(margin, cfg.extent_m - margin))
            cy = float(rng.uniform(margin, cfg.extent_m - margin))
            block = _irregular_polygon(
                rng, cx, cy, cfg.farmland_r_min, cfg.farmland_r_max
            )
            if any(block.intersects(other) for other in blocks):
                continue  # keep farmland blocks mutually disjoint
            blocks.append(block)
            break
    if len(blocks) != cfg.n_farmland:  # pragma: no cover - generous retry budget
        raise RuntimeError("could not place all farmland blocks without overlap")
    return blocks


def _build_parcels(
    rng: np.random.Generator, cfg: ScenarioConfig, farmland: Sequence[Polygon]
) -> list[Polygon]:
    """Parcels: four deliberately-constructed cases, the rest free-form."""
    anchor = farmland[0]
    parcels: list[Polygon] = [
        _contained_parcel(rng, anchor),
        _tangent_parcel(anchor),
        _overlapping_parcel(rng, anchor),
        _near_but_disjoint(rng, anchor),
    ]
    while len(parcels) < cfg.n_parcels:
        cx = float(rng.uniform(20.0, cfg.extent_m - 20.0))
        cy = float(rng.uniform(20.0, cfg.extent_m - 20.0))
        parcels.append(_irregular_polygon(rng, cx, cy, 15.0, 30.0))
    return parcels[: cfg.n_parcels]


def _build_buildings(rng: np.random.Generator, cfg: ScenarioConfig) -> list[Polygon]:
    return [
        Point(float(rng.uniform(20.0, cfg.extent_m - 20.0)),
              float(rng.uniform(20.0, cfg.extent_m - 20.0))).buffer(
            float(rng.uniform(6.0, 14.0)), quad_segs=4
        )
        for _ in range(cfg.n_buildings)
    ]


def _build_roads(rng: np.random.Generator, cfg: ScenarioConfig) -> list[LineString]:
    roads: list[LineString] = []
    for _ in range(cfg.n_roads):
        if rng.random() < 0.5:
            y = float(rng.uniform(0.0, cfg.extent_m))
            roads.append(LineString([(0.0, y), (cfg.extent_m, y)]))
        else:
            x = float(rng.uniform(0.0, cfg.extent_m))
            roads.append(LineString([(x, 0.0), (x, cfg.extent_m)]))
    return roads


def _build_facilities(rng: np.random.Generator, cfg: ScenarioConfig) -> list[Point]:
    return [
        Point(float(rng.uniform(20.0, cfg.extent_m - 20.0)),
              float(rng.uniform(20.0, cfg.extent_m - 20.0)))
        for _ in range(cfg.n_facilities)
    ]


# --------------------------------------------------------------------------- #
# Coordinate transformation
# --------------------------------------------------------------------------- #


def to_geographic(geometry: Any, from_epsg: int = 4547, to_epsg: int = 4326) -> Any:
    """Reproject a geometry ``from_epsg`` -> ``to_epsg`` with pyproj/PROJ.

    Real transformation rather than a scale-factor approximation: the CRS trap
    tasks are only meaningful if the degree-valued layer is a correct
    representation of the same ground geometry.
    """
    transformer = Transformer.from_crs(from_epsg, to_epsg, always_xy=True)
    return shapely_transform(transformer.transform, geometry)


# --------------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------------- #


def _round_coords(value: Any, digits: int = 6) -> Any:
    """Recursively round coordinates so serialization is stable."""
    if isinstance(value, (list, tuple)):
        return [_round_coords(v, digits) for v in value]
    if isinstance(value, float):
        return round(value, digits)
    return value


def feature_collection(
    geometries: Iterable[Any], layer: str, properties: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    """Build a standard GeoJSON FeatureCollection with stable key order."""
    features = []
    for index, (geom, props) in enumerate(zip(geometries, properties, strict=True)):
        record = mapping(geom)
        record["coordinates"] = _round_coords(record["coordinates"])
        payload = {"feature_index": index, **props}
        features.append(
            {"type": "Feature", "properties": payload, "geometry": record}
        )
    return {"type": "FeatureCollection", "name": layer, "features": features}


def write_json(path: Path, payload: Any) -> str:
    """Write JSON deterministically and return the file's SHA-256."""
    text = json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Ground truth
# --------------------------------------------------------------------------- #


def intersection_truth(
    parcels: Sequence[Polygon], farmland: Sequence[Polygon]
) -> list[dict[str, Any]]:
    """Per (parcel, farmland) truth: intersects flag and intersection area.

    This is the whole point of synthetic data: the truth is a **by-product of
    construction**, not a manual annotation, and it is computed with the same
    library the tools use.
    """
    rows: list[dict[str, Any]] = []
    for p_index, parcel in enumerate(parcels):
        for f_index, block in enumerate(farmland):
            inter = parcel.intersection(block)
            rows.append(
                {
                    "parcel_index": p_index,
                    "parcel_id": f"P{p_index + 1:03d}",
                    "farmland_index": f_index,
                    "intersects": bool(parcel.intersects(block)),
                    "intersection_area_m2": round(inter.area, 6),
                    "parcel_area_m2": round(parcel.area, 6),
                    "overlap_ratio": round(
                        inter.area / parcel.area if parcel.area else 0.0, 6
                    ),
                }
            )
    return rows


def distance_truth(
    parcels: Sequence[Polygon], layer: Sequence[Any], layer_name: str
) -> list[dict[str, Any]]:
    """Per-parcel nearest distance to a layer, in metres (projected CRS)."""
    rows = []
    for index, parcel in enumerate(parcels):
        nearest = min((parcel.distance(item) for item in layer), default=None)
        rows.append(
            {
                "parcel_index": index,
                "parcel_id": f"P{index + 1:03d}",
                "layer": layer_name,
                "min_distance_m": None if nearest is None else round(nearest, 6),
            }
        )
    return rows


def pair_distance_truth(
    parcels: Sequence[Polygon], pairs: Sequence[tuple[int, int]]
) -> list[dict[str, Any]]:
    """Straight-line distances between parcel pairs, in **metres**.

    Computed in the projected layer. The same two parcels also exist in the
    EPSG:4326 layer, where an unwary agent calling the same function gets
    degrees -- a value roughly five orders of magnitude smaller. These rows are
    the ground truth behind the CRS-trap tasks.
    """
    rows: list[dict[str, Any]] = []
    for a, b in pairs:
        rows.append(
            {
                "a_index": a,
                "a_id": f"P{a + 1:03d}",
                "b_index": b,
                "b_id": f"P{b + 1:03d}",
                "distance_m": round(parcels[a].distance(parcels[b]), 6),
            }
        )
    return rows


def district_summary(
    parcels: Sequence[Polygon], districts: Sequence[str]
) -> list[dict[str, Any]]:
    """Per-district parcel count and total area -- a multi-step aggregation truth."""
    buckets: dict[str, dict[str, float]] = {}
    for parcel, district in zip(parcels, districts, strict=True):
        bucket = buckets.setdefault(district, {"count": 0.0, "area": 0.0})
        bucket["count"] += 1
        bucket["area"] += parcel.area
    return [
        {
            "district_id": district,
            "parcel_count": int(values["count"]),
            "total_area_m2": round(values["area"], 6),
        }
        for district, values in sorted(buckets.items())
    ]


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def generate(cfg: ScenarioConfig, out_dir: Path) -> dict[str, Any]:
    """Generate every layer plus truth and metadata. Returns the manifest."""
    rng = np.random.default_rng(cfg.seed)
    farmland = _build_farmland(rng, cfg)
    parcels = _build_parcels(rng, cfg, farmland)
    buildings = _build_buildings(rng, cfg)
    roads = _build_roads(rng, cfg)
    facilities = _build_facilities(rng, cfg)

    def centroid_of(geom: Any) -> tuple[float, float]:
        c = geom.centroid
        return c.x, c.y

    layers: dict[str, tuple[list[Any], list[dict[str, Any]]]] = {
        "parcels": (
            parcels,
            [
                {
                    "parcel_id": f"P{i + 1:03d}",
                    "district_id": f"D{(i % 3) + 1:02d}",
                    "area_m2": round(g.area, 4),
                }
                for i, g in enumerate(parcels)
            ],
        ),
        "farmland": (
            farmland,
            [
                {
                    "farmland_id": f"F{i + 1:02d}",
                    "land_use": "farmland",
                    "area_m2": round(g.area, 4),
                }
                for i, g in enumerate(farmland)
            ],
        ),
        "buildings": (
            buildings,
            [
                {"building_id": f"B{i + 1:03d}", "area_m2": round(g.area, 4)}
                for i, g in enumerate(buildings)
            ],
        ),
        "roads": (
            roads,
            [
                {"road_id": f"R{i + 1:02d}", "length_m": round(g.length, 4)}
                for i, g in enumerate(roads)
            ],
        ),
        "facilities": (
            facilities,
            [
                {"facility_id": f"S{i + 1:02d}", "x": round(cx, 4), "y": round(cy, 4)}
                for i, g in enumerate(facilities)
                for cx, cy in [centroid_of(g)]
            ],
        ),
    }

    checksums: dict[str, str] = {}
    for name, (geoms, props) in layers.items():
        # Anchor at real CGCS2000 3-degree Gauss-Kruger coordinates before
        # reprojecting. The geometry is deliberately built on a local 0-1000 m
        # plane, which is fine for the truth table (a translation preserves
        # distances and areas) but NOT for reprojection: pyproj would read
        # (2.3, 16.8) as a projected coordinate near the equator and emit a
        # meaningless longitude/latitude pair.
        payload = feature_collection(
            (to_geographic(translate(g, xoff=ORIGIN_X, yoff=ORIGIN_Y)) for g in geoms),
            name,
            props,
        )
        checksums[name] = write_json(out_dir / f"{name}.json", payload)

    truth = {
        "parcel_vs_farmland": intersection_truth(parcels, farmland),
        "parcel_vs_buildings": distance_truth(parcels, buildings, "buildings"),
        "parcel_vs_facilities": distance_truth(parcels, facilities, "facilities"),
        "parcel_pair_distances": pair_distance_truth(
            parcels, [(0, 1), (0, 2), (4, 5), (9, 10)]
        ),
        "district_summary": district_summary(
            parcels, [f"D{(i % 3) + 1:02d}" for i in range(len(parcels))]
        ),
    }
    checksums["truth"] = write_json(out_dir / "truth.json", truth)

    manifest = {
        "config": asdict(cfg),
        "origin_epsg4547": {"x": ORIGIN_X, "y": ORIGIN_Y},
        "layer_crs": LAYER_CRS,
        "counts": {
            "parcels": len(parcels),
            "farmland": len(farmland),
            "buildings": len(buildings),
            "roads": len(roads),
            "facilities": len(facilities),
        },
        "deliberate_cases": {
            "P001": "contained in farmland F01 (intersection area == parcel area)",
            "P002": "tangent to farmland F01 (intersects == True, area == 0)",
            "P003": "partially overlapping farmland F01",
            "P004": "disjoint from farmland F01 but within ~60 m",
        },
        "sha256": checksums,
    }
    write_json(out_dir / "scenario.json", manifest)
    return manifest


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("bench/synthetic"),
        help="Output directory (default: bench/synthetic)",
    )
    parser.add_argument("--seed", type=int, default=ScenarioConfig.seed)
    parser.add_argument("--parcels", type=int, default=ScenarioConfig.n_parcels)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    cfg = ScenarioConfig(seed=args.seed, n_parcels=args.parcels)
    manifest = generate(cfg, args.out)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(f"\nwritten to: {args.out.resolve()}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
