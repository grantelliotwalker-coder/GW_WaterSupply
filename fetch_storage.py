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

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://www.bom.gov.au/waterdata/services"
TS_NAME = "DMQaQc.Merged.DailyMean.24HR"
PARAM = "Storage Volume"
CHUNK = 20

# No single Australian storage holds more than ~12.5 million ML (Lake Gordon).
# BoM occasionally serves absurd readings (e.g. Hinze Dam at 2.8e13 ML), which
# blew the national total into the billions of GL on some days. Ignore them.
MAX_PLAUSIBLE_ML = 20_000_000  # Reduced batch size to prevent server-side query timeouts

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; storage-data-fetch/1.0)",
    "Accept": "application/json",
}


def get_json(params, retries=6, base_delay=5, max_delay=90):
    """Fetches JSON from BoM KiWIS API with automatic retries.

    BoM's server has short outages where it returns HTTP 500 or times out for a
    minute or two. Retries back off 5s, 10s, 20s, 40s, 80s (~2.5 min total)
    so a brief blip doesn't fail the whole run. 4xx errors (other than 429)
    mean the request itself is wrong, so those are not retried."""
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
        except (TimeoutError, urllib.error.URLError, http.client.HTTPException,
                ConnectionError, socket.timeout, json.JSONDecodeError, UnicodeDecodeError) as e:
            if isinstance(e, urllib.error.HTTPError) and 400 <= e.code < 500 and e.code != 429:
                raise
            if attempt == retries - 1:
                print(f"Request failed after {retries} attempts: {e}", file=sys.stderr)
                raise
            sleep_time = min(base_delay * 2 ** attempt, max_delay)
            print(f"  [Attempt {attempt + 1}/{retries}] Request failed ({e}). Retrying in {sleep_time}s...", file=sys.stderr)
            time.sleep(sleep_time)


def local_today():
    """History is keyed by the Sydney calendar date, not UTC. GitHub often starts
    scheduled runs hours late, and with UTC dates a late run could cross midnight
    UTC, leaving a day with no entry and the next day with two runs."""
    return datetime.now(ZoneInfo("Australia/Sydney")).date().isoformat()


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


def fetch_values(ids, retries=6):
    raw = get_json({
        "request": "getTimeseriesValues",
        "ts_id": ",".join(ids),
        "period": "P30D",
        "returnfields": "Timestamp,Value",
        "metadata": "true",
        "md_returnfields": "ts_id",
    }, retries=retries)
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
    batches = [series[i:i + CHUNK] for i in range(0, len(series), CHUNK)]
    failed = []
    for n, batch in enumerate(batches):
        try:
            values = fetch_values([str(s["ts_id"]) for s in batch])
        except Exception as e:
            print(f"  WARNING: batch {n + 1}/{len(batches)} failed: {e} — will retry at the end", file=sys.stderr)
            failed.append(batch)
            continue
        rows.extend(rows_from_batch(batch, values))
        print(f"  {min((n + 1) * CHUNK, len(series))}/{len(series)} series processed", file=sys.stderr)
        time.sleep(0.3)  # be polite to BoM's server

    if failed:
        print(f"Retrying {len(failed)} failed batch(es) after a 60s pause...", file=sys.stderr)
        time.sleep(60)
        for batch in failed:
            try:
                rows.extend(rows_from_batch(batch, fetch_values([str(s["ts_id"]) for s in batch])))
            except Exception as e:
                print(f"  WARNING: batch still failing ({e}); {len(batch)} series missing this run", file=sys.stderr)
            time.sleep(1)

    if not rows:
        raise SystemExit("No rows were returned — aborting without overwriting data.json")

    # Sanity check: if this run is missing a big chunk of stations compared with
    # the last good run, BoM was probably having problems. Saving it would log a
    # fake drop in the national total, so keep yesterday's files instead.
    try:
        with open("data.json") as f:
            prev_raw = json.load(f).get("raw_count") or 0
    except (FileNotFoundError, json.JSONDecodeError):
        prev_raw = 0
    if prev_raw and len(rows) < 0.9 * prev_raw:
        raise SystemExit(f"Only {len(rows)} readings returned vs {prev_raw} last run — "
                         "aborting without overwriting data.json")

    finish(rows)


def rows_from_batch(batch, values):
    rows = []
    for s in batch:
        pt = values.get(str(s["ts_id"]))
        if not pt:
            continue
        try:
            v = float(pt[1])
        except (TypeError, ValueError):
            continue
        if v < 0 or v > MAX_PLAUSIBLE_ML:
            if v > MAX_PLAUSIBLE_ML:
                print(f"  WARNING: ignoring implausible reading {v:,.0f} ML for {s.get('station_name')} "
                      f"({s.get('station_no')})", file=sys.stderr)
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
    return rows


def finish(rows):
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

    today = local_today()
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

    try:
        backfill_gaps(kept)
    except Exception as e:
        print(f"WARNING: backfill failed ({e}); today's data is unaffected.", file=sys.stderr)


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
    }, retries=2)
    return to_objects(raw)


def fetch_extra_parameters_and_history(kept_stations):
    print("Fetching additional parameters for each station...", file=sys.stderr)

    station_series = {}
    all_extra_ids = []

    def list_station(no):
        series = list_all_series_for_station(no)
        wanted = [s for s in series if s.get("parametertype_name") in OTHER_PARAMS]
        station_series[no] = wanted
        for s in wanted:
            all_extra_ids.append((no, str(s["ts_id"]), s.get("parametertype_name"), s.get("ts_unitname")))

    skipped = []
    for idx, st in enumerate(kept_stations):
        no = st["no"]
        try:
            list_station(no)
        except Exception as e:
            print(f"  skip {no}: {e}", file=sys.stderr)
            skipped.append(no)
        if (idx + 1) % 25 == 0:
            print(f"  listed {idx + 1}/{len(kept_stations)} stations", file=sys.stderr)
        time.sleep(0.15)

    if skipped:
        print(f"Retrying {len(skipped)} skipped station(s) after a 60s pause...", file=sys.stderr)
        time.sleep(60)
        still = 0
        for no in skipped:
            try:
                list_station(no)
            except Exception:
                still += 1
            time.sleep(0.5)
        print(f"  {len(skipped) - still} recovered, {still} still missing", file=sys.stderr)

    print(f"Found {len(all_extra_ids)} extra parameter series across {len(station_series)} stations. Fetching values...", file=sys.stderr)

    values_by_ts = {}
    all_ts_ids = [t[1] for t in all_extra_ids]
    for i in range(0, len(all_ts_ids), CHUNK):
        batch_ids = all_ts_ids[i:i + CHUNK]
        try:
            values_by_ts.update(fetch_values(batch_ids, retries=3))
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

    today = local_today()
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


# ---------------------------------------------------------------------------
# Backfill: fill in any days missing from history.json / station_history.json
# using BoM's own past daily readings. Runs at the end of every update, so a
# failed or skipped day gets repaired automatically on the next good run.
# ---------------------------------------------------------------------------
BACKFILL_MAX_DAYS = 400   # don't reach back further than this
STALE_DAYS = 30           # same rule as the live fetch: latest reading within 30 days


def fetch_range(ids, start, end):
    """Daily readings for each ts_id between two dates: {ts_id: [(date, value), ...]}"""
    raw = get_json({
        "request": "getTimeseriesValues",
        "ts_id": ",".join(ids),
        "from": start,
        "to": end,
        "returnfields": "Timestamp,Value",
        "metadata": "true",
        "md_returnfields": "ts_id",
    }, retries=4)
    out = {}
    for series in raw if isinstance(raw, list) else []:
        pts = []
        for ts, val in series.get("data", []):
            try:
                pts.append((ts[:10], float(val)))
            except (TypeError, ValueError):
                continue
        out[str(series["ts_id"])] = sorted(pts)
    return out


def value_as_of(points, day):
    """Mimic what a live run on `day` would have seen: the latest reading dated
    before that day and no more than STALE_DAYS old. points = sorted [(date, v)]."""
    cutoff = date.fromisoformat(day)
    best = None
    for d, v in points:
        dd = date.fromisoformat(d)
        if dd >= cutoff:
            break
        if (cutoff - dd).days <= STALE_DAYS and 0 <= v <= MAX_PLAUSIBLE_ML:
            best = v
    return best


def missing_dates(existing, first, last):
    have = set(existing)
    d = date.fromisoformat(first)
    end = date.fromisoformat(last)
    out = []
    while d <= end:
        if d.isoformat() not in have:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def drop_implausible(history, station_history):
    """Remove history days whose total is wildly above normal, and station points
    above MAX_PLAUSIBLE_ML. The backfill then refills those days with clean data."""
    totals = sorted(h["total_ML"] for h in history)
    if totals:
        median = totals[len(totals) // 2]
        bad = [h["date"] for h in history if h["total_ML"] > 1.5 * median]
        if bad:
            print(f"Removing {len(bad)} implausible day(s) from history.json: {', '.join(bad)}", file=sys.stderr)
            history = [h for h in history if h["total_ML"] <= 1.5 * median]
    removed = 0
    for no, series in station_history.items():
        clean = [p for p in series if (p.get("volume_ML") or 0) <= MAX_PLAUSIBLE_ML]
        removed += len(series) - len(clean)
        station_history[no] = clean
    if removed:
        print(f"Removed {removed} implausible station reading(s) from station_history.json", file=sys.stderr)
    return history, station_history


def backfill_gaps(kept_stations):
    today = local_today()
    earliest_allowed = (date.fromisoformat(today) - timedelta(days=BACKFILL_MAX_DAYS)).isoformat()

    with open("history.json") as f:
        history = json.load(f)
    try:
        with open("station_history.json") as f:
            station_history = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        station_history = {}

    # Drop any days poisoned by an implausible reading so they get rebuilt below.
    history, station_history = drop_implausible(history, station_history)

    hist_dates = [h["date"] for h in history]
    if not hist_dates:
        return
    first = max(min(hist_dates), earliest_allowed)
    total_gaps = missing_dates(hist_dates, first, today)

    # Each station's own gaps (a station may be missing days the total isn't)
    station_gaps = {}
    for st in kept_stations:
        series = station_history.get(st["no"], [])
        if series:
            s_first = max(series[0]["date"], earliest_allowed)
            g = missing_dates([p["date"] for p in series], s_first, today)
            if g:
                station_gaps[st["no"]] = g

    if not total_gaps and not station_gaps:
        print("Backfill: no missing days.", file=sys.stderr)
        return

    all_gap_days = set(total_gaps) | {d for g in station_gaps.values() for d in g}
    start = (date.fromisoformat(min(all_gap_days)) - timedelta(days=STALE_DAYS + 1)).isoformat()
    print(f"Backfill: {len(total_gaps)} day(s) missing from history.json, "
          f"{len(station_gaps)} station(s) with gaps; fetching readings from {start}...", file=sys.stderr)

    ids = [str(st["ts_id"]) for st in kept_stations]
    readings = {}
    for i in range(0, len(ids), CHUNK):
        try:
            readings.update(fetch_range(ids[i:i + CHUNK], start, today))
        except Exception as e:
            print(f"  backfill batch failed: {e}", file=sys.stderr)
        time.sleep(0.3)

    # Only trust the backfill if we got readings for nearly every station, so a
    # flaky BoM day can't write artificially low totals.
    if len(readings) < 0.9 * len(ids):
        print(f"Backfill: only got {len(readings)}/{len(ids)} stations — skipping, will try next run.",
              file=sys.stderr)
        return

    now = datetime.now(timezone.utc).isoformat()
    added = 0
    for day in total_gaps:
        vals = [value_as_of(readings.get(str(st["ts_id"]), []), day) for st in kept_stations]
        vals = [v for v in vals if v is not None]
        if len(vals) < 0.9 * len(kept_stations):
            continue
        total = sum(vals)
        history.append({
            "date": day,
            "generated_at": now,
            "total_ML": total,
            "total_GL": total / 1000,
            "count": len(vals),
            "backfilled": True,
        })
        added += 1
    history.sort(key=lambda h: h["date"])

    s_added = 0
    for st in kept_stations:
        gaps = station_gaps.get(st["no"])
        pts = readings.get(str(st["ts_id"]))
        if not gaps or not pts:
            continue
        series = station_history[st["no"]]
        for day in gaps:
            v = value_as_of(pts, day)
            if v is not None:
                series.append({"date": day, "volume_ML": v, "backfilled": True})
                s_added += 1
        series.sort(key=lambda p: p["date"])
        station_history[st["no"]] = series[-730:]

    with open("history.json", "w") as f:
        json.dump(history[-730:], f, indent=2)
    with open("station_history.json", "w") as f:
        json.dump(station_history, f, indent=2)
    print(f"Backfill: added {added} day(s) to history.json and {s_added} station-day point(s) "
          f"to station_history.json.", file=sys.stderr)


if __name__ == "__main__":
    main()
