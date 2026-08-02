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


def normalise_coord(lat, lon):
    """Fix the occasional sign error in BoM's coordinates (all of Australia is at
    negative latitude, positive longitude). Returns (lat, lon) or (None, None)."""
    try:
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError):
        return None, None
    return -abs(lat), abs(lon)


def classify_state(lat, lon):
    """Rough state/territory classification from coordinates. BoM doesn't return
    state directly, so this uses approximate boundaries — good enough for grouping,
    not survey-accurate near borders. Some BoM records carry a stray sign on
    longitude (e.g. -153.9 instead of 153.9); since all of Australia sits at
    positive longitude and negative latitude, we normalise both before classifying."""
    try:
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError):
        return None
    lat = -abs(lat)   # Australia is entirely in the southern hemisphere
    lon = abs(lon)    # Australia is entirely in the eastern hemisphere
    if lat < -39.5:
        return "TAS"
    if -36.0 <= lat <= -34.5 and 148.7 <= lon <= 149.4:
        return "ACT"
    if lon < 129:
        return "WA"
    if lon < 141:
        return "NT" if lat > -26 else "SA"
    if lon <= 154:
        if lat > -29:
            return "QLD"
        return "NSW" if lat > -37 else "VIC"
    return None


# Preferred display name when a dedup group's largest-volume entry has a less
# recognisable name than a sibling in the same group (e.g. "Hideaway Bay" vs
# the dam's common name, "Warragamba"). Keyed by the group's matched_on value.
DISPLAY_NAME_OVERRIDES = {
    "station_no:212243": "Warragamba Dam",
}


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
            lat, lon = normalise_coord(s.get("station_latitude"), s.get("station_longitude"))
            rows.append({
                "name": s.get("station_name"),
                "no": s.get("station_no"),
                "ts_id": s.get("ts_id"),
                "time": pt[0],
                "volume_ML": v,
                "lat": lat,
                "lon": lon,
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
        chosen = dict(group_sorted[0])
        override_key = f"station_no:{key}"
        if override_key in DISPLAY_NAME_OVERRIDES:
            chosen["name"] = DISPLAY_NAME_OVERRIDES[override_key]
        pass1_kept.append(chosen)
        if len(group) > 1:
            duplicate_groups.append({
                "matched_on": override_key,
                "kept": chosen,
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
        chosen = dict(group_sorted[0])
        if key in DISPLAY_NAME_OVERRIDES:
            chosen["name"] = DISPLAY_NAME_OVERRIDES[key]
        kept.append(chosen)
        if len(group) > 1:
            duplicate_groups.append({
                "matched_on": key,
                "kept": chosen,
                "excluded": group_sorted[1:],
            })

    total_ML = sum(r["volume_ML"] for r in kept)
    latest = max(r["time"] for r in kept)

    for r in kept:
        r["state"] = classify_state(r.get("lat"), r.get("lon"))

    stations_sorted = sorted(
        kept,
        key=lambda r: (r["state"] or "ZZ", -r["volume_ML"])
    )

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
        "stations": stations_sorted,
    }

    with open("data.json", "w") as f:
        json.dump(output, f, indent=2)

    # --- Append this run to history.json so the site can chart the national trend ---
    history_path = "history.json"
    try:
        with open(history_path) as f:
            history = json.load(f)
        if not isinstance(history, list):
            history = []
    except (FileNotFoundError, json.JSONDecodeError):
        history = []

    today = output["generated_at"][:10]
    history = [h for h in history if h.get("date") != today]  # avoid same-day dupes on re-run
    history.append({
        "date": today,
        "generated_at": output["generated_at"],
        "total_ML": total_ML,
        "total_GL": total_ML / 1000,
        "count": len(kept),
    })
    history.sort(key=lambda h: h["date"])
    history = history[-730:]  # cap at ~2 years of daily points

    with open(history_path, "w") as f:
        json.dump(history, f, indent=2)

    print(f"Wrote data.json: {len(kept)} stations after dedup (raw {len(rows)}), "
          f"{total_ML/1000:,.1f} GL total (raw {raw_total_ML/1000:,.1f} GL)", file=sys.stderr)
    print(f"Appended to history.json ({len(history)} points on file)", file=sys.stderr)

    # --- Fetch every other parameter each kept station reports (level, discharge,
    # rainfall, water quality, etc.), and track per-station history over time ---
    fetch_extra_parameters_and_history(kept)


# Parameters worth checking for on top of Storage Volume. Not every station has
# every one of these — we just ask and keep whatever comes back.
OTHER_PARAMS = [
    "Water Course Level", "Water Course Discharge", "Storage Level",
    "Rainfall", "Electrical Conductivity", "Water Temperature",
    "Turbidity", "pH", "Dissolved Oxygen", "Groundwater Level",
]


def list_all_series_for_station(station_no):
    raw = get_json({
        "request": "getTimeseriesList",
        "station_no": station_no,
        "ts_name": TS_NAME,
        "returnfields": "station_no,parametertype_name,ts_id,ts_unitname",
    })
    return to_objects(raw)


def fetch_extra_parameters_and_history(kept_stations):
    print("Fetching additional parameters for each station...", file=sys.stderr)

    # Discover every relevant series per station (one list call per station).
    station_series = {}   # station_no -> [{parametertype_name, ts_id, ts_unitname}, ...]
    all_extra_ids = []     # (station_no, ts_id, param_name, unit)
    for idx, st in enumerate(kept_stations):
        no = st["no"]
        try:
            series = list_all_series_for_station(no)
        except Exception as e:
            print(f"  skip {no}: {e}", file=sys.stderr)
            continue
        wanted = [s for s in series if s.get("parametertype_name") in OTHER_PARAMS]
        station_series[no] = wanted
        for s in wanted:
            all_extra_ids.append((no, str(s["ts_id"]), s.get("parametertype_name"), s.get("ts_unitname")))
        if (idx + 1) % 25 == 0:
            print(f"  listed {idx + 1}/{len(kept_stations)} stations", file=sys.stderr)
        time.sleep(0.15)

    print(f"Found {len(all_extra_ids)} extra parameter series across {len(station_series)} stations. Fetching values...", file=sys.stderr)

    # Fetch the latest value for every extra series, in chunks.
    values_by_ts = {}
    all_ts_ids = [t[1] for t in all_extra_ids]
    for i in range(0, len(all_ts_ids), CHUNK):
        batch_ids = all_ts_ids[i:i + CHUNK]
        try:
            values_by_ts.update(fetch_values(batch_ids))
        except Exception as e:
            print(f"  values batch failed: {e}", file=sys.stderr)
        time.sleep(0.2)

    # Attach results to each station.
    by_station_no = {st["no"]: st for st in kept_stations}
    for no, ts_id, param, unit in all_extra_ids:
        pt = values_by_ts.get(ts_id)
        if not pt:
            continue
        try:
            v = float(pt[1])
        except (TypeError, ValueError):
            continue
        st = by_station_no.get(no)
        if st is None:
            continue
        st.setdefault("parameters", []).append({
            "parameter": param,
            "unit": unit,
            "value": v,
            "time": pt[0],
        })

    # Re-save data.json now that stations carry their extra parameters.
    with open("data.json") as f:
        output = json.load(f)
    output["stations"] = kept_stations
    with open("data.json", "w") as f:
        json.dump(output, f, indent=2)

    # --- Per-station history: one file, keyed by station number ---
    hist_path = "station_history.json"
    try:
        with open(hist_path) as f:
            station_history = json.load(f)
        if not isinstance(station_history, dict):
            station_history = {}
    except (FileNotFoundError, json.JSONDecodeError):
        station_history = {}

    today = datetime.now(timezone.utc).date().isoformat()
    for st in kept_stations:
        no = st["no"]
        point = {"date": today, "volume_ML": st["volume_ML"]}
        for p in st.get("parameters", []):
            point[p["parameter"]] = p["value"]
        series = station_history.get(no, [])
        series = [p for p in series if p.get("date") != today]
        series.append(point)
        series.sort(key=lambda p: p["date"])
        station_history[no] = series[-730:]

    with open(hist_path, "w") as f:
        json.dump(station_history, f, indent=2)

    print(f"Wrote station_history.json ({len(station_history)} stations tracked)", file=sys.stderr)


if __name__ == "__main__":
    main()
