#!/usr/bin/env python3
"""Build config/zipcodes.npz - the US ZIP code table behind Settings > SYSTEM > Location.

Source: the GeoNames postal code data set (https://www.geonames.org, Creative Commons
Attribution 4.0): the United States plus Puerto Rico, the US Virgin Islands, Guam, American
Samoa and the Northern Mariana Islands. Military APO / FPO codes are left out: they are
mail addresses, not places.

    python3 tools/build_zipcodes.py                # download and rebuild the bundled file
    python3 tools/build_zipcodes.py --source DIR   # use US.zip, PR.zip, ... (or .txt) already in DIR
"""
from __future__ import annotations

import argparse
import io
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pisdr import config  # noqa: E402

URL = "https://download.geonames.org/export/zip/{country}.zip"
COUNTRIES = ("US", "PR", "VI", "GU", "AS", "MP")     # GeoNames files that hold US ZIP codes
# Tab-separated columns of a GeoNames file.
COUNTRY, ZIP, TOWN, STATE, LAT, LON = 0, 1, 2, 4, 9, 10


def read_source(country: str, source: Path | None) -> str | None:
    """The text of one country's file, or None if it is not there (only the US file is required)."""
    if source is not None:
        plain, packed = source / f"{country}.txt", source / f"{country}.zip"
        if plain.exists():
            return plain.read_text(encoding="utf-8")
        if not packed.exists():
            return None
        raw = packed.read_bytes()
    else:
        url = URL.format(country=country)
        print(f"  downloading {url}")
        try:
            with urllib.request.urlopen(url, timeout=120) as reply:
                raw = reply.read()
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            return None
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        return archive.read(f"{country}.txt").decode("utf-8")


def parse(text: str, table: dict[int, tuple[float, float, str]] | None = None
          ) -> dict[int, tuple[float, float, str]]:
    """Add the rows of one file to {ZIP code: (lat, lon, "Town ST")}; the first row for a code wins."""
    table = {} if table is None else table
    for line in text.splitlines():
        fields = line.split("\t")
        if len(fields) <= LON:
            continue
        code, town = fields[ZIP].strip(), " ".join(fields[TOWN].split())
        # A territory's file has no state column worth showing; its two-letter code is the "state".
        state = fields[STATE].strip() if fields[COUNTRY] == "US" else fields[COUNTRY].strip()
        if not (len(code) == 5 and code.isdigit()) or not town or not state:
            continue
        try:
            lat, lon = float(fields[LAT]), float(fields[LON])
        except ValueError:
            continue
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            continue
        table.setdefault(int(code), (lat, lon, f"{town} {state}"))
    return table


def build(source: Path | None = None) -> dict[str, np.ndarray]:
    table: dict[int, tuple[float, float, str]] = {}
    for country in COUNTRIES:
        text = read_source(country, source)
        if text is None:
            if country == "US":
                raise SystemExit("the US file is missing")
            print(f"  {country}: not found, left out")
            continue
        before = len(table)
        parse(text, table)
        print(f"  {country}: {len(table) - before} ZIP codes")
    if not table:
        raise SystemExit("no ZIP codes found")
    codes = sorted(table)
    names = "\n".join(table[code][2] for code in codes).encode("utf-8")
    return {"zip": np.asarray(codes, dtype=np.uint32),
            "lat": np.asarray([table[code][0] for code in codes], dtype=np.float32),
            "lon": np.asarray([table[code][1] for code in codes], dtype=np.float32),
            "names": np.frombuffer(names, dtype=np.uint8)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", type=Path, help="folder of GeoNames <country>.zip or .txt files (no download)")
    parser.add_argument("--out", type=Path, default=config.ZIPCODES_FILE)
    args = parser.parse_args(argv)
    arrays = build(args.source)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "wb") as out:              # a file object: savez would add ".npz" to a name
        np.savez_compressed(out, **arrays)
    print(f"wrote {args.out}: {len(arrays['zip'])} ZIP codes, {args.out.stat().st_size / 1024:.0f} kB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
