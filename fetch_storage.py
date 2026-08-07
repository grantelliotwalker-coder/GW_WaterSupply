#!/usr/bin/env python3
"""
Fetches every Storage Volume timeseries from BoM Water Data Online (KiWIS),
takes the latest reading for each, and writes data.json for the website to read.

Run this from GitHub Actions (or locally) — never from a browser.
"""
import http.client
import json
import re
import socket
import sys
import time

from datetime import datetime, timezone
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://www.bom.gov.au/waterdata/services"
TS_NAME = "DMQaQc.Merged.DailyMean.24HR"
PARAM = "Storage Volume"
CHUNK = 20  # Reduced batch size to prevent server-side query timeouts

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; storage-data-fetch/1.0)",
    "Accept": "application/json",
}


def get_json(params, retries=3, backoff_factor=2):
    """Fetches JSON from BoM KiWIS API with automatic retries on timeout/network failures."""
    qs = urllib.parse.urlencode({
        "service": "kisters",
        "type": "queryServices",
        "datasource": "0",
        "format": "json",
        **params,
    })
    req = urllib.request.Request(f"{BASE}?{qs}", headers=HEADERS)

    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                body = resp.read().decode("utf-8")
            data = json.loads(body)
            if isinstance(data, dict) and data.get("type") == "error":
                raise RuntimeError(data.get("message", "Water Data Online returned an error"))
            return data
        except (TimeoutError, urllib.error.URLError, http.client.RemoteDisconnected, socket.timeout) as e:
            if attempt == retries - 1:
                print(f"Request failed after {retries} attempts: {e}", file=sys.stderr)
                raise
            sleep_time = backoff_factor ** (attempt + 1)
            print(f"  [Attempt {attempt + 1}/{retries}] Request timed out/failed ({e}). Retrying in {sleep_time}s...", file=sys.stderr)
            time.sleep(sleep_time)


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


# A handful of stations carry genuinely wrong coordinates in BoM's own source
# data (not just a sign error — the value itself is off by several degrees).
# Verified against each dam's known real-world location.
COORD_OVERRIDES = {
    "141012A": (-26.7719, 152.9603),   # Ewen Maddock Dam, QLD
    "145033A": (-27.9421, 152.8394),   # Wyaralong Dam, QLD
}


def normalise_coord(lat, lon, station_no=None):
    """Fix coordinate errors in BoM's data: sign flips (all of Australia is at
    negative latitude, positive longitude) and a small list of known bad values.
    Returns (lat, lon) or (None, None)."""
    if station_no in COORD_OVERRIDES:
        return COORD_OVERRIDES[station_no]
    try:
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError):
        return None, None
    return -abs(lat), abs(lon)


# The NSW/VIC border roughly follows the Murray River, which curves — it is NOT
# a straight line of latitude. A flat threshold wrongly tags much of central and
# western Victoria (Bendigo, Horsham, Goulburn Weir, Waranga Basin, Dartmouth...)
# as NSW, since large parts of Victoria sit further north than the Snowy Mountains
# section of the border. This is a rough piecewise-linear trace of the border's
# latitude at a handful of longitudes, interpolated between them.
NSW_VIC_BORDER = [
    (141.0, -34.0), (142.0, -34.5), (143.0, -35.7), (144.0, -36.0),
    (145.0, -36.05), (146.0, -36.1), (147.0, -36.2), (148.0, -36.6),
    (149.0, -37.0), (150.0, -37.5), (151.0, -37.5),
]


def nsw_vic_border_lat(lon):
    pts = NSW_VIC_BORDER
    if lon <= pts[0][0]:
        return pts[0][1]
    if lon >= pts[-1][0]:
        return pts[-1][1]
    for (lon1, lat1), (lon2, lat2) in zip(pts, pts[1:]):
        if lon1 <= lon <= lon2:
            frac = (lon - lon1) / (lon2 - lon1)
            return lat1 + frac * (lat2 - lat1)
    return pts[-1][1]


# A few stations that still need a manual state fix even after the curved-border
# approximation above (e.g. genuinely wrong source coordinates).
STATE_OVERRIDES = {}


def classify_state(lat, lon, station_no=None):
    """Rough state/territory classification from coordinates."""
    if station_no in STATE_OVERRIDES:
        return STATE_OVERRIDES[station_no]
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
        return "NSW" if lat > nsw_vic_border_lat(lon) else "VIC"
    return None


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
        try:
            values = fetch_values([str(s["ts_id"]) for s in batch])
        except Exception as e:
            print(f"  WARNING: Failed to fetch batch starting at index {i}: {e}", file=sys.stderr)
            continue

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
            lat, lon = normalise_coord(s.get("station_latitude"), s.get("station_longitude"), s.get("station_no"))
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
        r["state"] = classify_state(r.get("lat"), r.get("lon"), r.get("no"))
        lat, lon = r.get("lat"), r.get("lon")
        if lat is not None and lon is not None and not (-44 <= lat <= -9 and 112 <= lon <= 154):
            print(f"  WARNING: {r['name']} ({r['no']}) has out-of-Australia coordinates "
                  f"lat={lat}, lon={lon} — check COORD_OVERRIDES", file=sys.stderr)

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

    history_path = "history.json"
    try:
        with open(history_path) as f:
            history = json.load(f)
        if not isinstance(history, list):
            history = []
    except (FileNotFoundError, json.JSONDecodeError):
        history = []

    today = output["generated_at"][:10]
    history = [h for h in history if h.get("date") != today]
    history.append({
        "date": today,
        "generated_at": output["generated_at"],
        "total_ML": total_ML,
        "total_GL": total_ML / 1000,
        "count": len(kept),
    })
    history.sort(key=lambda h: h["date"])
    history = history[-730:]

    with open(history_path, "w") as f:
        json.dump(history, f, indent=2)

    print(f"Wrote data.json: {len(kept)} stations after dedup (raw {len(rows)}), "
          f"{total_ML/1000:,.1f} GL total (raw {raw_total_ML/1000:,.1f} GL)", file=sys.stderr)
    print(f"Appended to history.json ({len(history)} points on file)", file=sys.stderr)

    try:
        fetch_extra_parameters_and_history(kept)
    except Exception as e:
        print(f"WARNING: fetch_extra_parameters_and_history failed: {e}", file=sys.stderr)
        print("data.json and history.json are still valid and will be committed; "
              "station-level parameters/history were not updated this run.", file=sys.stderr)


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

    station_series = {}
    all_extra_ids = []
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

    values_by_ts = {}
    all_ts_ids = [t[1] for t in all_extra_ids]
    for i in range(0, len(all_ts_ids), CHUNK):
        batch_ids = all_ts_ids[i:i + CHUNK]
        try:
            values_by_ts.update(fetch_values(batch_ids))
        except Exception as e:
            print(f"  values batch failed: {e}", file=sys.stderr)
        time.sleep(0.2)

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

    with open("data.json") as f:
        output = json.load(f)
    output["stations"] = kept_stations
    with open("data.json", "w") as f:
        json.dump(output, f, indent=2)

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
