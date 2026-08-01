#!/usr/bin/env python3
"""
Fetches every Storage Volume timeseries from BoM Water Data Online (KiWIS),
takes the latest reading for each, and writes data.json for the website to read.

Run this from GitHub Actions (or locally) — never from a browser.
"""
import json
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
        "returnfields": "station_no,station_name,ts_id,ts_unitname",
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


def normalise_name(name):
    """Loose key for grouping likely-duplicate stations reporting the same storage."""
    if not name:
        return ""
    n = name.lower()
    for junk in ["dam", "storage", "reservoir", "weir", "wsl", "level", "logged",
                 "water", "top", "full", "supply", "vill", "villiage", "village"]:
        n = n.replace(junk, "")
    return "".join(ch for ch in n if ch.isalnum())


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
            })
        print(f"  {min(i + CHUNK, len(series))}/{len(series)} series processed", file=sys.stderr)
        time.sleep(0.3)  # be polite to BoM's server

    if not rows:
        raise SystemExit("No rows were returned — aborting without overwriting data.json")

    raw_total_ML = sum(r["volume_ML"] for r in rows)

    # --- Dedup: group rows that are almost certainly the same physical storage ---
    groups = {}
    for r in rows:
        key = normalise_name(r["name"])
        groups.setdefault(key, []).append(r)

    kept = []
    duplicate_groups = []
    for key, group in groups.items():
        if len(group) == 1:
            kept.append(group[0])
            continue
        # Keep the single largest reading as the storage's value; record the rest
        # so a human can double-check the choice.
        group_sorted = sorted(group, key=lambda r: -r["volume_ML"])
        kept.append(group_sorted[0])
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
