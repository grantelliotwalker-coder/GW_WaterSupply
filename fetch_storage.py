#!/usr/bin/env python3
"""
Fetches every Storage Volume timeseries from BoM Water Data Online (KiWIS),
takes the latest reading for each, and writes data.json for the website to read.

Run this from GitHub Actions (or locally) — never from a browser.
"""
import json
import re
import sys
import time
from datetime import datetime, timezone
import urllib.request
import urllib.parse

BASE = "https://www.bom.gov.au/waterdata/services"
TS_NAME = "DMQaQc.Merged.DailyMean.24HR"
PARAM = "Storage Volume"
CHUNK = 40

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; storage-data-fetch/1.0)",
    "Accept": "application/json",
}


def get_json(params):
    qs = urllib.parse.urlencode({
        "service": "kisters",
        "type": "queryServices",
        "datasource": "0",
        "format": "json",
        **params,
    })
    req = urllib.request.Request(f"{BASE}?{qs}", headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8")
    data = json.loads(body)
    if isinstance(data, dict) and data.get("type") == "error":
        raise RuntimeError(data.get("message", "Water Data Online returned an error"))
    return data


def to_objects(table):
    if not isinstance(table, list) or len(table) < 2:
        return []
    head = table[0]
    return [dict(zip(head, row)) for row in table[1:]]


def list_storage_series():
    raw = get_json({
        "request": "getTimeseriesList",
        "parametertype_name": PARAM,
        "ts_name": TS_NAME,
        "returnfields": "station_no,station_name,ts_id,ts_unitname,station_latitude,station_longitude",
    })
    return to_objects(raw)


def fetch_values(ids):
    raw = get_json({
        "request": "getTimeseriesValues",
        "ts_id": ",".join(ids),
        "period": "P30D",
        "returnfields": "Timestamp,Value",
        "metadata": "true",
        "md_returnfields": "ts_id",
    })
    out = {}
    for series in raw if isinstance(raw, list) else []:
        pts = [p for p in series.get("data", []) if p[1] is not None]
        if pts:
            out[str(series["ts_id"])] = pts[-1]
    return out


def base_station_no(no):
    """Strip trailing .N suffixes so 212243.3 and 212243 are recognised as the same site."""
    if not no:
        return ""
    return re.sub(r"\.\d+$", "", str(no))


def normalise_name(name):
    """Loose key for grouping likely-duplicate stations reporting the same storage."""
    if not name:
        return ""
    n = name.lower()
    for junk in ["dam", "storage", "reservoir", "weir", "wsl", "level", "logged",
                 "water", "top", "full", "supply", "vill", "villiage", "village",
                 "on", "scada", "wl", "at", "intake", "hw"]:
        n = re.sub(rf"\b{junk}\b", "", n)
    return "".join(ch for ch in n if ch.isalnum())


# Known same-storage groups that neither number-prefix matching nor loose name
# matching can safely catch automatically (genuinely different station numbers,
# genuinely different names, but the same physical reservoir). Verified by hand.
KNOWN_DUPLICATE_GROUPS = [
    ["lake argyle", "argyle vill", "argyle water level"],
]


def known_group_key(name):
    if not name:
        return None
    low = name.lower()
    for i, group in enumerate(KNOWN_DUPLICATE_GROUPS):
        if any(phrase in low for phrase in group):
            return f"known:{i}"
    return None


def main():
    print("Fetching storage series list...", file=sys.stderr)
    series = list_storage_series()
    series = [s for s in series if not s.get("ts_unitname") or "ML" in s["ts_unitname"] or "megal" in s["ts_unitname"].lower()]
    print(f"Found {len(series)} storage series.", file=sys.stderr)

    rows = []
    for i in range(0, len(series), CHUNK):
        batch = series[i:i + CHUNK]
        values = fetch_values([str(s["ts_id"]) for s in batch])
        for s in batch:
            pt = values.get(str(s["ts_id"]))
            if not pt:
                continue
            try:
                v = float(pt[1])
            except (TypeError, ValueError):
                continue
            if v < 0:
                continue
            rows.append({
                "name": s.get("station_name"),
                "no": s.get("station_no"),
                "ts_id": s.get("ts_id"),
                "time": pt[0],
                "volume_ML": v,
                "lat": s.get("station_latitude"),
                "lon": s.get("station_longitude"),
            })
        print(f"  {min(i + CHUNK, len(series))}/{len(series)} series processed", file=sys.stderr)
        time.sleep(0.3)  # be polite to BoM's server

    if not rows:
        raise SystemExit("No rows were returned — aborting without overwriting data.json")

    raw_total_ML = sum(r["volume_ML"] for r in rows)

    # --- Pass 1: group by base station number (handles the vast majority of dupes:
    # 212243 vs 212243.3, 215212 vs 215212.1 vs 215212.3, etc.) ---
    by_number = {}
    for r in rows:
        by_number.setdefault(base_station_no(r["no"]), []).append(r)

    pass1_kept = []
    duplicate_groups = []
    for key, group in by_number.items():
        group_sorted = sorted(group, key=lambda r: -r["volume_ML"])
        pass1_kept.append(group_sorted[0])
        if len(group) > 1:
            duplicate_groups.append({
                "matched_on": f"station_no:{key}",
                "kept": group_sorted[0],
                "excluded": group_sorted[1:],
            })

    # --- Pass 2: catch remaining dupes that share a manually-verified name pattern
    # but have genuinely different station numbers (e.g. Lake Argyle monitored from
    # three separate gauge sites) ---
    by_known = {}
    unmatched = []
    for r in pass1_kept:
        k = known_group_key(r["name"])
        if k:
            by_known.setdefault(k, []).append(r)
        else:
            unmatched.append(r)

    kept = list(unmatched)
    for key, group in by_known.items():
        group_sorted = sorted(group, key=lambda r: -r["volume_ML"])
        kept.append(group_sorted[0])
        if len(group) > 1:
            duplicate_groups.append({
                "matched_on": key,
                "kept": group_sorted[0],
                "excluded": group_sorted[1:],
            })

    total_ML = sum(r["volume_ML"] for r in kept)
    latest = max(r["time"] for r in kept)

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "latest_reading": latest,
        "count": len(kept),
        "total_ML": total_ML,
        "total_GL": total_ML / 1000,
        "raw_count": len(rows),
        "raw_total_ML": raw_total_ML,
        "raw_total_GL": raw_total_ML / 1000,
        "duplicate_groups": duplicate_groups,
        "stations": sorted(kept, key=lambda r: -r["volume_ML"]),
    }

    with open("data.json", "w") as f:
        json.dump(output, f, indent=2)

    print(f"Wrote data.json: {len(kept)} stations after dedup (raw {len(rows)}), "
          f"{total_ML/1000:,.1f} GL total (raw {raw_total_ML/1000:,.1f} GL)", file=sys.stderr)


if __name__ == "__main__":
    main()
