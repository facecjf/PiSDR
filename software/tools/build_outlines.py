#!/usr/bin/env python3
"""Build config/outlines.npz - the coastlines, lakes and borders drawn on the ADS-B map.

Source: Natural Earth (public domain, naturalearthdata.com), downloaded as GeoJSON from its
GitHub repository. The bundled file was made with the defaults (1:50m data), which is plenty
for a 100 nm view and a little angular at 10 nm.

    python3 tools/build_outlines.py                 # download and rebuild the bundled file
    python3 tools/build_outlines.py --source DIR    # use .geojson files already in DIR
    python3 tools/build_outlines.py --scale 10m     # finer coastlines, file about 7x the size (2.5 MB)
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pisdr import config, outlines  # noqa: E402

URL = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/{tag}/geojson/{name}.geojson"
TAG = "v5.1.2"
LAYERS = (                                   # (file name without the scale prefix, kind)
    ("coastline", outlines.COAST),
    ("lakes", outlines.LAKE),
    ("admin_0_boundary_lines_land", outlines.BORDER),
    ("admin_1_states_provinces_lines", outlines.STATE),
)


def read_layer(name: str, source: Path | None, tag: str) -> dict:
    if source is not None:
        return json.loads((source / f"{name}.geojson").read_text(encoding="utf-8"))
    url = URL.format(tag=tag, name=name)
    print(f"  downloading {url}")
    with urllib.request.urlopen(url, timeout=120) as reply:
        return json.loads(reply.read().decode("utf-8"))


def lines_of(geometry: dict | None):
    """Every line (list of [lon, lat]) in a GeoJSON geometry; polygon rings count as lines."""
    if not geometry:
        return
    kind, coords = geometry.get("type"), geometry.get("coordinates", [])
    if kind == "LineString":
        yield coords
    elif kind in ("MultiLineString", "Polygon"):
        yield from coords
    elif kind == "MultiPolygon":
        for polygon in coords:
            yield from polygon
    elif kind == "GeometryCollection":
        for part in geometry.get("geometries", []):
            yield from lines_of(part)


def quantise(line) -> np.ndarray | None:
    """(n, 2) int16 [lat, lon] in hundredths of a degree with repeated points dropped."""
    points = np.asarray([(p[1], p[0]) for p in line], dtype=np.float64)
    if len(points) < 2:
        return None
    points[:, 0] = np.clip(points[:, 0], -90.0, 90.0)
    points[:, 1] = np.clip(points[:, 1], -180.0, 180.0)
    cells = np.round(points * outlines.SCALE).astype(np.int16)
    keep = np.ones(len(cells), dtype=bool)
    keep[1:] = np.any(cells[1:] != cells[:-1], axis=1)
    cells = cells[keep]
    return cells if len(cells) >= 2 else None


def build(scale: str = "50m", source: Path | None = None, tag: str = TAG) -> dict[str, np.ndarray]:
    lines: list[np.ndarray] = []
    kinds: list[int] = []
    for layer, kind in LAYERS:
        name = f"ne_{scale}_{layer}"
        before = len(lines)
        for feature in read_layer(name, source, tag).get("features", []):
            for line in lines_of(feature.get("geometry")):
                cells = quantise(line)
                if cells is not None:
                    lines.append(cells)
                    kinds.append(kind)
        points = sum(len(line) for line in lines[before:])
        print(f"  {name}: {len(lines) - before} lines, {points} points")
    if not lines:
        raise SystemExit("no lines found")
    everything = np.concatenate(lines)
    start = np.concatenate(([0], np.cumsum([len(line) for line in lines]))).astype(np.int32)
    box = np.array([(line[:, 0].min(), line[:, 1].min(), line[:, 0].max(), line[:, 1].max())
                    for line in lines], dtype=np.int16)
    return {"lat": everything[:, 0].copy(), "lon": everything[:, 1].copy(), "start": start,
            "kind": np.asarray(kinds, dtype=np.uint8), "box": box}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scale", choices=("110m", "50m", "10m"), default="50m")
    parser.add_argument("--source", type=Path, help="folder of ne_<scale>_*.geojson files (no download)")
    parser.add_argument("--tag", default=TAG, help="Natural Earth release to download")
    parser.add_argument("--out", type=Path, default=config.OUTLINES_FILE)
    args = parser.parse_args(argv)
    arrays = build(args.scale, args.source, args.tag)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "wb") as out:              # a file object: savez would add ".npz" to a name
        np.savez_compressed(out, **arrays)
    print(f"wrote {args.out}: {len(arrays['kind'])} lines, {len(arrays['lat'])} points, "
          f"{args.out.stat().st_size / 1024:.0f} kB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
