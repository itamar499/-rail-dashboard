from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
from datetime import datetime, timedelta
import json
import os
import re
import threading
import time
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, request, send_from_directory
from israelrailapi import TrainSchedule
import israelrailapi.api as rail_api
import israelrailapi.schedule as rail_schedule
import israelrailapi.train_station as rail_station
import requests

app = Flask(__name__)
IL_TZ = ZoneInfo("Asia/Jerusalem")

STATION_MAP = {
    680: "Jerusalem Yitzhak Navon",
    3700: "Tel Aviv Savidor Center",
    9650: "Netivot",
}

# Broad hub coverage to capture lines quickly without querying every station-to-station pair.
BOARD_HUBS = [
    # Major line terminals and high-value termini first (better chance before timeout):
    3500, 1600, 1840, 1280, 7320, 7300, 7500, 5900, 9600, 9000, 9800,
    # Tel Aviv / center and branch endpoints:
    3700, 4600, 4900, 3600, 4100, 4250, 4170, 4210, 2940, 2960, 3310, 3300,
    # Jerusalem and airport corridor:
    680, 6500, 6700, 8600, 400, 480,
    # South/center extra endpoints:
    5000, 5010, 5200, 5410, 5800, 8550,
    # Station-specific catchers:
    2100, 2300, 1220, 9650, 9700,
]

BOARD_CACHE_TTL_SECONDS = 25
_BOARD_CACHE = {}


def _safe_translate_station(station_name):
    """
    Compatibility shim for israelrailapi translate_station bug.
    Some library versions use STATIONS.stations even though STATIONS is a dict.
    """
    key = str(station_name).lower()
    stations = getattr(rail_station, "STATIONS", {})
    station_index = getattr(rail_station, "STATION_INDEX", {})

    if isinstance(stations, dict) and key in stations:
        return key
    return station_index[rail_station.cleanup_name(key)]


rail_station.translate_station = _safe_translate_station
rail_schedule.translate_station = _safe_translate_station

# Ensure upstream rail API calls don't hang for long periods.
_original_requests_post = rail_api.requests.post
_rail_key_lock = threading.Lock()
_rail_key_cache = {
    "value": os.environ.get("ISRAEL_RAILWAYS_API_KEY")
    or rail_api.DEFAULT_HEADERS.get("ocp-apim-subscription-key"),
    "expires_at": 0,
}

RAIL_BROWSER_HEADERS = {
    "User-Agent": rail_api.USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7",
}

RAIL_API_EXTRA_HEADERS = {
    "Origin": "https://www.rail.co.il",
    "Referer": "https://www.rail.co.il/",
    "Accept": "application/json, text/plain, */*",
}
RAIL_API_DIRECT_BASE = "https://rail-api.rail.co.il"
RAIL_API_PROXY_BASE = os.environ.get(
    "ISRAEL_RAILWAYS_API_PROXY_BASE", ""
).rstrip("/")
_rail_proxy_until = 0

RAIL_KEY_PATTERNS = [
    re.compile(r'ocp-apim-subscription-key["\']?\s*[:=]\s*["\']([a-fA-F0-9]{32,64})["\']'),
    re.compile(r'subscription[-_]?key["\']?\s*[:=]\s*["\']([a-fA-F0-9]{32,64})["\']', re.IGNORECASE),
    re.compile(r'([a-fA-F0-9]{32,64})'),
]


def _extract_rail_key(text):
    for idx, pattern in enumerate(RAIL_KEY_PATTERNS):
        match = pattern.search(text or "")
        if match:
            candidate = match.group(1)
            if idx == 2 and len(candidate) < 32:
                continue
            return candidate
    return None


def _fetch_rail_home():
    response = requests.get(
        "https://www.rail.co.il/",
        headers=RAIL_BROWSER_HEADERS,
        timeout=(4, 10),
    )
    response.raise_for_status()
    html = response.text
    script_urls = []
    for script_path in re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', html, flags=re.IGNORECASE):
        if ".js" not in script_path.lower():
            continue
        script_urls.append(urljoin("https://www.rail.co.il/", script_path))
    return html, script_urls


def _refresh_rail_api_key():
    html, script_urls = _fetch_rail_home()

    home_key = _extract_rail_key(html)
    if home_key:
        return home_key

    prioritized = sorted(
        script_urls,
        key=lambda url: (0 if "main" in url.lower() else 1, len(url)),
    )
    for script_url in prioritized[:8]:
        try:
            script_response = requests.get(
                script_url,
                headers=RAIL_BROWSER_HEADERS,
                timeout=(4, 10),
            )
            script_response.raise_for_status()
        except Exception:
            continue
        script_key = _extract_rail_key(script_response.text)
        if script_key:
            return script_key
    raise RuntimeError("Could not locate a valid Rail API subscription key")


def _get_rail_api_key(force_refresh=False):
    env_key = os.environ.get("ISRAEL_RAILWAYS_API_KEY")
    if env_key:
        return env_key
    if _rail_key_cache["value"]:
        return _rail_key_cache["value"]

    now_ts = time.time()
    if not force_refresh and _rail_key_cache["value"] and _rail_key_cache["expires_at"] > now_ts:
        return _rail_key_cache["value"]

    with _rail_key_lock:
        now_ts = time.time()
        if not force_refresh and _rail_key_cache["value"] and _rail_key_cache["expires_at"] > now_ts:
            return _rail_key_cache["value"]
        try:
            key = _refresh_rail_api_key()
            _rail_key_cache["value"] = key
            _rail_key_cache["expires_at"] = now_ts + (6 * 60 * 60)
            return key
        except Exception as exc:
            app.logger.warning("Rail API key refresh failed: %s", exc)
            if _rail_key_cache["value"]:
                _rail_key_cache["expires_at"] = now_ts + (20 * 60)
                return _rail_key_cache["value"]
            raise


def _build_rail_headers(existing_headers=None, force_refresh=False):
    headers = dict(existing_headers or {})
    headers.update(RAIL_API_EXTRA_HEADERS)
    headers.setdefault("Content-Type", "application/json")
    headers.setdefault("User-Agent", rail_api.USER_AGENT)
    headers["ocp-apim-subscription-key"] = _get_rail_api_key(force_refresh=force_refresh)
    return headers


def _rail_proxy_url(url):
    if not isinstance(url, str) or not RAIL_API_PROXY_BASE:
        return None
    if not url.startswith(RAIL_API_DIRECT_BASE):
        return None
    return f"{RAIL_API_PROXY_BASE}{url[len(RAIL_API_DIRECT_BASE):]}"


def _requests_post_with_timeout(*args, **kwargs):
    global _rail_proxy_until
    kwargs.setdefault("timeout", (3, 8))

    url = kwargs.get("url")
    if not url and args:
        url = args[0]

    if isinstance(url, str) and "rail-api.rail.co.il/rjpa/api/v1/" in url:
        base_headers = kwargs.get("headers") or rail_api.DEFAULT_HEADERS
        kwargs["headers"] = _build_rail_headers(base_headers, force_refresh=False)
        proxy_url = _rail_proxy_url(url)

        if proxy_url and _rail_proxy_until > time.time():
            proxy_args = list(args)
            if "url" in kwargs:
                kwargs["url"] = proxy_url
            elif proxy_args:
                proxy_args[0] = proxy_url
            else:
                kwargs["url"] = proxy_url
            return _original_requests_post(*proxy_args, **kwargs)

        response = _original_requests_post(*args, **kwargs)
        if response.status_code != 403:
            return response

        if not proxy_url:
            return response

        app.logger.warning("Direct Israel Railways API returned 403, retrying via proxy")
        _rail_proxy_until = time.time() + (15 * 60)
        proxy_kwargs = dict(kwargs)
        proxy_args = list(args)
        if "url" in proxy_kwargs:
            proxy_kwargs["url"] = proxy_url
        elif proxy_args:
            proxy_args[0] = proxy_url
        else:
            proxy_kwargs["url"] = proxy_url
        return _original_requests_post(*proxy_args, **proxy_kwargs)

    return _original_requests_post(*args, **kwargs)


rail_api.requests.post = _requests_post_with_timeout


def _has_hebrew_text(value):
    return any("\u0590" <= c <= "\u05FF" for c in str(value))


def _is_valid_date(value):
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _is_valid_time(value):
    if not value:
        return False
    if len(value) >= 5:
        value = value[:5]
    try:
        datetime.strptime(value, "%H:%M")
        return True
    except ValueError:
        return False


def _now_local():
    # Use Israel local time regardless of server runtime timezone.
    return datetime.now(IL_TZ).replace(tzinfo=None)


def _normalize_date(value):
    if _is_valid_date(value):
        return value
    return _now_local().strftime("%Y-%m-%d")


def _normalize_time(value):
    if not value:
        return _now_local().strftime("%H:%M")
    candidate = value[:5]
    if _is_valid_time(candidate):
        return candidate
    return _now_local().strftime("%H:%M")


def _parse_iso_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _safe_add_minutes(dt_value, minutes):
    if dt_value is None or minutes is None:
        return dt_value
    return dt_value + timedelta(minutes=minutes)


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _extract_train_realtime(train, station_id):
    station_key = str(station_id)
    eta_diff_times = train.data.get("etaDiffTimes") or []
    delay_minutes = None
    for item in eta_diff_times:
        if str(item.get("stationId")) != station_key:
            continue
        delay_minutes = _to_int(item.get("difMin"))
        if delay_minutes is not None:
            break

    raw_position = train.data.get("trainPosition")
    train_position = None
    if isinstance(raw_position, dict):
        train_position = {
            "current_last_station": get_station_name(raw_position.get("currentLastStation"))
            if raw_position.get("currentLastStation")
            else None,
            "next_station": get_station_name(raw_position.get("nextStation"))
            if raw_position.get("nextStation")
            else None,
            "calc_diff_minutes": _to_int(raw_position.get("calcDiffMinutes")),
        }

    return {
        "delay_minutes": delay_minutes,
        "has_realtime": bool(eta_diff_times or train_position),
        "train_position": train_position,
    }


def _coerce_time_with_date(value, date_str):
    """Convert HH:MM to ISO datetime using date_str; keep ISO values as-is."""
    if not value:
        return None
    value = str(value).strip()
    if "T" in value:
        return value
    if len(value) >= 5 and ":" in value:
        return f"{date_str}T{value[:5]}:00"
    return None


def _find_station_pass_time(train, station_id, date_str):
    """
    Find the train's timestamp and platform at the requested station.
    Prefer stopStations (usually ISO), fallback to routeStations (often HH:MM),
    and finally train source departure when station is train.src.
    """
    station_key = str(station_id)

    for stop in train.data.get("stopStations", []) or []:
        if str(stop.get("stationId")) != station_key:
            continue
        return {
            "time": stop.get("departureTime") or stop.get("arrivalTime"),
            "platform": stop.get("platform"),
        }

    for stop in train.data.get("routeStations", []) or []:
        if str(stop.get("stationId")) != station_key:
            continue
        raw_time = stop.get("departureTime") or stop.get("arrivalTime")
        return {
            "time": _coerce_time_with_date(raw_time, date_str),
            "platform": stop.get("platform"),
        }

    if str(train.src) == station_key:
        return {
            "time": train.departure,
            "platform": train.platform,
        }

    return None


def _board_key(line_start, line_end, departure_time, train_number=None):
    """
    Use train number + departure time as the primary identity.
    Fallbacks are used only when train number is missing.
    """
    line_start = str(line_start or "").strip()
    line_end = str(line_end or "").strip()
    dep = str(departure_time or "").strip()
    train_num = str(train_number or "").strip()
    if train_num:
        return ("run", train_num, dep)
    return ("line", line_start, line_end, dep)


def _line_name_from_stops(stops, fallback_src, fallback_dst):
    if stops:
        return f"{stops[0]['name']} - {stops[-1]['name']}"
    return f"{fallback_src} - {fallback_dst}"


def get_station_name(sid, raw_name=None):
    """Resolve the best available station name."""
    if not sid:
        return raw_name or "Unknown"
    sid_str = str(sid)

    if raw_name and _has_hebrew_text(raw_name):
        return raw_name

    try:
        from israelrailapi.stations import STATIONS

        sinfo = STATIONS.get(sid_str)
        if sinfo and isinstance(sinfo, dict):
            if sinfo.get("Heb"):
                return sinfo["Heb"]
            if sinfo.get("Eng"):
                return sinfo["Eng"]
    except Exception:
        pass

    try:
        from israelrailapi.train_station import translate_station

        resolved = translate_station(sid_str)
        if resolved and not str(resolved).isdigit():
            return resolved
    except Exception:
        pass

    if sid in STATION_MAP:
        return STATION_MAP[sid]
    return raw_name if raw_name else sid_str


def _load_all_stations():
    stations = []
    try:
        from israelrailapi.stations import STATIONS

        for sid, info in STATIONS.items():
            if not isinstance(info, dict):
                continue
            sid_int = int(sid)
            heb = info.get("Heb")
            eng = info.get("Eng")
            display_name = heb or eng or str(sid_int)
            stations.append(
                {
                    "id": sid_int,
                    "name": display_name,
                    "heb": heb,
                    "eng": eng,
                }
            )
    except Exception:
        for sid, name in STATION_MAP.items():
            stations.append({"id": sid, "name": name, "heb": None, "eng": name})

    stations.sort(key=lambda s: (s["name"] or "").lower())
    return stations


def route_to_dict(route):
    """Convert TrainRoute and TrainRoutePart to dict for JSON serialization."""
    trains = []
    for t in route.trains:
        realtime = _extract_train_realtime(t, t.src)
        stops = []
        raw_stops = t.data.get("stopStations", [])

        for s in raw_stops:
            sid = s.get("stationId")
            raw_heb = s.get("stationNameHeb")
            sname = get_station_name(sid, raw_heb)
            stops.append(
                {
                    "id": str(sid),
                    "name": sname,
                    "departure": s.get("departureTime"),
                    "arrival": s.get("arrivalTime"),
                    "platform": s.get("platform"),
                }
            )

        src_name = get_station_name(t.src)
        dst_name = get_station_name(t.dst)

        if not stops or stops[0]["name"] != src_name:
            stops.insert(
                0,
                {
                    "id": str(t.src),
                    "name": src_name,
                    "departure": t.departure,
                    "arrival": None,
                    "platform": t.platform,
                },
            )

        if stops[-1]["name"] != dst_name:
            stops.append(
                {
                    "id": str(t.dst),
                    "name": dst_name,
                    "departure": None,
                    "arrival": t.arrival,
                    "platform": t.dst_platform,
                }
            )

        trains.append(
            {
                "train_number": t.data.get("trainNumber"),
                "departure": t.departure,
                "arrival": t.arrival,
                "src": src_name,
                "dst": dst_name,
                "platform": t.platform,
                "dst_platform": t.dst_platform,
                "delay_minutes": realtime.get("delay_minutes"),
                "has_realtime": realtime.get("has_realtime"),
                "stops": stops,
            }
        )

    return {
        "start_time": route.start_time,
        "end_time": route.end_time,
        "trains": trains,
    }


def _filter_routes_from_request_time(routes, date_str, time_str):
    """Drop routes that start before the user-requested date/time."""
    requested_dt = _parse_iso_datetime(f"{date_str}T{time_str}:00")
    if requested_dt is None:
        return routes

    filtered = []
    for route in routes:
        start_dt = _parse_iso_datetime(getattr(route, "start_time", None))
        if start_dt is None or start_dt >= requested_dt:
            filtered.append(route)
    return filtered


def _query_routes_once(from_id, to_id, date_str, time_str):
    try:
        return TrainSchedule.query(from_id, to_id, date_str, time_str)
    except Exception:
        return TrainSchedule.query(
            STATION_MAP.get(from_id, from_id),
            STATION_MAP.get(to_id, to_id),
            date_str,
            time_str,
        )


_GTFS_FALLBACK_CACHE = {"loaded": False, "data": None}

GTFS_STATION_ID_OVERRIDES = {
    # Israel Railways API station id -> Ministry of Transport GTFS stop_id.
    # Names are not always identical between the two public datasets.
    "3700": "37358",  # Tel Aviv Savidor Center / Tel Aviv Center
    "4600": "37350",  # Tel Aviv HaShalom / HaShalom
    "4900": "37292",  # Tel Aviv HaHagana
    "8600": "37306",  # Ben Gurion Airport / Natbag
}


def _is_rail_forbidden_error(exc):
    message = str(exc or "")
    return "403" in message and "searchTrain" in message


def _load_gtfs_fallback_data():
    if _GTFS_FALLBACK_CACHE["loaded"]:
        return _GTFS_FALLBACK_CACHE["data"]

    data_path = os.path.join(app.root_path, "assets", "rail_gtfs_compact.json")
    if not os.path.exists(data_path):
        _GTFS_FALLBACK_CACHE["loaded"] = True
        _GTFS_FALLBACK_CACHE["data"] = None
        return None

    with open(data_path, "r", encoding="utf-8") as fp:
        data = json.load(fp)
    _GTFS_FALLBACK_CACHE["loaded"] = True
    _GTFS_FALLBACK_CACHE["data"] = data
    return data


def _normalize_gtfs_station_name(value):
    text = str(value or "")
    replacements = {
        '"': "",
        "'": "",
        "׳": "",
        "״": "",
        "/": " ",
        "-": " ",
        "קריית": "קרית",
        "המפרץ": "מפרץ",
        "נמל תעופה בן גוריון": "נתבג",
        "בן גוריון": "נתבג",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    text = re.sub(r"\([^)]*\)", " ", text)
    text = text.replace("תחנת", " ").replace("רכבת", " ")
    return " ".join(text.split())


def _gtfs_station_tokens(value):
    return set(_normalize_gtfs_station_name(value).split())


def _resolve_gtfs_station(payload, station_id):
    station_map = payload.get("station_map", {})
    key = str(station_id)
    if key in station_map:
        return station_map[key]
    if key in GTFS_STATION_ID_OVERRIDES:
        return GTFS_STATION_ID_OVERRIDES[key]

    station_name = get_station_name(station_id)
    station_tokens = _gtfs_station_tokens(station_name)
    if not station_tokens:
        return None

    best = None
    for sid, stop in (payload.get("stops") or {}).items():
        stop_name = stop.get("name")
        stop_tokens = _gtfs_station_tokens(stop_name)
        if not stop_tokens:
            continue
        score = len(station_tokens & stop_tokens) / len(station_tokens | stop_tokens)
        if _normalize_gtfs_station_name(station_name) == _normalize_gtfs_station_name(stop_name):
            score += 1
        elif _normalize_gtfs_station_name(stop_name) in _normalize_gtfs_station_name(station_name):
            score += 0.5
        if best is None or score > best[0]:
            best = (score, sid)

    if best and best[0] >= 0.3:
        return best[1]
    return None


def _seconds_from_hhmm(time_str):
    hour, minute = [int(part) for part in str(time_str)[:5].split(":")]
    return (hour * 3600) + (minute * 60)


def _iso_from_date_and_seconds(base_date, seconds_total):
    day_offset, sec_in_day = divmod(int(seconds_total), 24 * 3600)
    ts = datetime.combine(base_date, datetime.min.time()) + timedelta(days=day_offset, seconds=sec_in_day)
    return ts.isoformat(timespec="seconds")


def _service_active_on_date(service_row, date_obj):
    if not service_row:
        return False
    ymd = date_obj.strftime("%Y%m%d")
    if ymd < service_row.get("start", "") or ymd > service_row.get("end", ""):
        return False
    # Python Monday=0 ... Sunday=6, which matches the order in compact data.
    return bool(service_row.get("days", [0, 0, 0, 0, 0, 0, 0])[date_obj.weekday()])


def _query_routes_gtfs_fallback(from_id, to_id, date_str, time_str, all_day=False, limit=25):
    if all_day and limit == 25:
        limit = 200

    payload = _load_gtfs_fallback_data()
    if not payload:
        return []

    stops_by_id = payload.get("stops", {})
    calendar = payload.get("calendar", {})
    trips = payload.get("trips", [])

    from_station = _resolve_gtfs_station(payload, from_id)
    to_station = _resolve_gtfs_station(payload, to_id)
    if not from_station or not to_station:
        return []

    requested_seconds = _seconds_from_hhmm(time_str)
    requested_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    from_name = get_station_name(from_id)
    to_name = get_station_name(to_id)

    routes = []
    for trip in trips:
        if not _service_active_on_date(calendar.get(trip.get("svc")), requested_date):
            continue

        compact_stops = trip.get("stops") or []
        from_idx = None
        to_idx = None
        for idx, stop_row in enumerate(compact_stops):
            stop_id = stop_row[0]
            if stop_id == from_station and from_idx is None:
                from_idx = idx
            if stop_id == to_station:
                to_idx = idx
                if from_idx is not None and to_idx > from_idx:
                    break
        if from_idx is None or to_idx is None or to_idx <= from_idx:
            continue

        dep_seconds = compact_stops[from_idx][2]
        arr_seconds = compact_stops[to_idx][1]
        if not all_day and dep_seconds < requested_seconds:
            continue

        trip_slice = compact_stops[from_idx: to_idx + 1]
        stops = []
        for idx, (sid, arr_sec, dep_sec) in enumerate(trip_slice):
            stop_name = (stops_by_id.get(sid) or {}).get("name") or sid
            stops.append(
                {
                    "id": str(sid),
                    "name": stop_name,
                    "departure": _iso_from_date_and_seconds(requested_date, dep_sec),
                    "arrival": _iso_from_date_and_seconds(requested_date, arr_sec),
                    "platform": None,
                }
            )
            if idx == 0:
                stops[-1]["arrival"] = None
            if idx == len(trip_slice) - 1:
                stops[-1]["departure"] = None

        dep_iso = _iso_from_date_and_seconds(requested_date, dep_seconds)
        arr_iso = _iso_from_date_and_seconds(requested_date, arr_seconds)
        train_number = str(trip.get("id", "")).split("_")[0]

        routes.append(
            {
                "start_time": dep_iso,
                "end_time": arr_iso,
                "trains": [
                    {
                        "train_number": train_number or None,
                        "departure": dep_iso,
                        "arrival": arr_iso,
                        "src": from_name,
                        "dst": to_name,
                        "platform": None,
                        "dst_platform": None,
                        "delay_minutes": None,
                        "has_realtime": False,
                        "stops": stops,
                    }
                ],
            }
        )

    routes.sort(key=lambda row: row.get("start_time", ""))
    return routes[:limit]


def _route_unique_key(route):
    trains = getattr(route, "trains", []) or []
    first_train = trains[0] if trains else None
    first_train_num = None
    if first_train is not None:
        first_train_num = (first_train.data or {}).get("trainNumber")
    return (
        getattr(route, "start_time", None),
        getattr(route, "end_time", None),
        first_train_num,
        len(trains),
    )


def _query_routes_all_day(from_id, to_id, date_str):
    # The upstream API usually returns a short "next trips" window per query.
    # Sample the day in chunks and merge unique routes.
    anchor_times = [f"{h:02d}:00" for h in range(0, 24, 2)]
    unique = {}

    for anchor in anchor_times:
        try:
            routes = _query_routes_once(from_id, to_id, date_str, anchor)
        except Exception as exc:
            if _is_rail_forbidden_error(exc):
                raise
            continue
        for route in routes:
            key = _route_unique_key(route)
            unique[key] = route

    results = list(unique.values())
    results.sort(
        key=lambda r: (
            _parse_iso_datetime(getattr(r, "start_time", None)) or datetime.max,
            _parse_iso_datetime(getattr(r, "end_time", None)) or datetime.max,
        )
    )
    return results


def _build_train_stops(train):
    stops = []
    raw_route_stops = train.data.get("routeStations", []) or []

    # Prefer routeStations: it usually represents the full train line endpoints.
    for stop in raw_route_stops:
        sid = stop.get("stationId")
        if sid is None:
            continue
        name = get_station_name(sid, stop.get("stationNameHeb"))
        stops.append(
            {
                "id": str(sid),
                "name": name,
                "time": stop.get("arrivalTime") or stop.get("departureTime"),
            }
        )

    # Fallback to stopStations when routeStations is missing.
    if not stops:
        raw_stops = train.data.get("stopStations", [])
        for stop in raw_stops:
            sid = stop.get("stationId")
            name = get_station_name(sid, stop.get("stationNameHeb"))
            stops.append(
                {
                    "id": str(sid),
                    "name": name,
                    "time": stop.get("arrivalTime") or stop.get("departureTime"),
                }
            )

        src_name = get_station_name(train.src)
        dst_name = get_station_name(train.dst)
        if not stops or stops[0]["name"] != src_name:
            stops.insert(
                0,
                {"id": str(train.src), "name": src_name, "time": train.departure},
            )
        if stops[-1]["name"] != dst_name:
            stops.append(
                {"id": str(train.dst), "name": dst_name, "time": train.arrival},
            )

    # Deduplicate neighboring stations.
    unique = []
    for stop in stops:
        if not unique or unique[-1]["name"] != stop["name"]:
            unique.append(stop)
    return unique


@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/style.css")
def css():
    return send_from_directory(".", "style.css")


@app.route("/favicon.svg")
def favicon():
    return send_from_directory(".", "favicon.svg")

@app.route("/app-icon.png")
def app_icon():
    return send_from_directory(".", "app-icon.png")


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"}), 200


@app.route("/api/stations")
def get_stations():
    q = (request.args.get("q") or "").strip().lower()
    stations = _load_all_stations()
    if q:
        stations = [
            station
            for station in stations
            if q in str(station.get("name", "")).lower()
            or q in str(station.get("heb", "")).lower()
            or q in str(station.get("eng", "")).lower()
            or q in str(station.get("id", ""))
        ]
    return jsonify(stations)


@app.route("/api/routes/<int:from_id>/<int:to_id>")
def get_routes(from_id, to_id):
    now = _now_local()
    date_str = _normalize_date(request.args.get("date", now.strftime("%Y-%m-%d")))
    time_str = _normalize_time(request.args.get("time", now.strftime("%H:%M")))
    all_day = request.args.get("all_day", "0") == "1"

    try:
        app.logger.info(
            "Querying route %s -> %s (%s %s all_day=%s)",
            from_id,
            to_id,
            date_str,
            time_str,
            all_day,
        )
        if all_day:
            results = _query_routes_all_day(from_id, to_id, date_str)
        else:
            results = _query_routes_once(from_id, to_id, date_str, time_str)
            results = _filter_routes_from_request_time(results, date_str, time_str)
        return jsonify([route_to_dict(r) for r in results])
    except Exception as exc:
        if _is_rail_forbidden_error(exc):
            app.logger.warning("Primary rail API returned 403, using GTFS fallback")
            fallback_routes = _query_routes_gtfs_fallback(
                from_id,
                to_id,
                date_str,
                time_str,
                all_day=all_day,
            )
            # Never bubble up 403 to the UI when the upstream rail API is blocked.
            # If fallback has no matching trips, return an empty successful result.
            return jsonify(fallback_routes)
        app.logger.exception("Error fetching route schedules")
        return jsonify({"error": str(exc), "details": "Error fetching schedules"}), 500


@app.route("/api/station-board/<station_id>")
def get_station_board(station_id):
    try:
        now = _now_local()
        date_str = now.strftime("%Y-%m-%d")
        minutes_window_raw = request.args.get("minutes", "30")
        only_upcoming = request.args.get("upcoming", "1") != "0"
        fast_mode = request.args.get("fast", "1") != "0"

        try:
            minutes_window = int(minutes_window_raw)
        except ValueError:
            minutes_window = 90
        minutes_window = max(5, min(minutes_window, 720))

        time_str = now.strftime("%H:%M") if only_upcoming else "00:00"
        max_time = now + timedelta(minutes=minutes_window)

        cache_slot = int(time.time() // BOARD_CACHE_TTL_SECONDS)
        cache_key = (str(station_id), minutes_window, only_upcoming, fast_mode, cache_slot)
        cached = _BOARD_CACHE.get(cache_key)
        if cached is not None:
            return jsonify(cached)

        available_station_ids = {station["id"] for station in _load_all_stations()}
        hubs = [
            hub for hub in BOARD_HUBS
            if hub in available_station_ids and str(hub) != str(station_id)
        ]
        if fast_mode:
            hubs = hubs[:12]

        def query_hub(hub):
            departures = []
            try:
                results = TrainSchedule.query(station_id, hub, date_str, time_str)
                for route in results:
                    for train in route.trains:
                        station_pass = _find_station_pass_time(train, station_id, date_str)
                        if not station_pass:
                            continue

                        train_num = train.data.get("trainNumber")
                        departure_time = station_pass.get("time")
                        departure_dt = _parse_iso_datetime(departure_time)
                        realtime = _extract_train_realtime(train, station_id)
                        effective_departure_dt = _safe_add_minutes(
                            departure_dt,
                            realtime["delay_minutes"],
                        )
                        effective_departure_time = (
                            effective_departure_dt.isoformat()
                            if effective_departure_dt
                            else departure_time
                        )

                        if only_upcoming and effective_departure_dt:
                            if effective_departure_dt < now or effective_departure_dt > max_time:
                                continue

                        eta_minutes = None
                        if departure_dt:
                            eta_minutes = int((departure_dt - now).total_seconds() // 60)
                        effective_eta_minutes = eta_minutes
                        if effective_departure_dt is not None:
                            effective_eta_minutes = int((effective_departure_dt - now).total_seconds() // 60)
                        stops = _build_train_stops(train)
                        line_src = stops[0]["name"] if stops else get_station_name(train.src)
                        line_dst = stops[-1]["name"] if stops else get_station_name(train.dst)
                        line_name = _line_name_from_stops(
                            stops,
                            line_src,
                            line_dst,
                        )

                        departures.append(
                            {
                                "unique_key": _board_key(
                                    line_src,
                                    line_dst,
                                    departure_time,
                                    train_num,
                                ),
                                "train_number": train_num,
                                "src": line_src,
                                "dest": line_dst,
                                "line_name": line_name,
                                "time": departure_time,
                                "effective_time": effective_departure_time,
                                "platform": station_pass.get("platform") or train.platform,
                                "eta_minutes": eta_minutes,
                                "effective_eta_minutes": effective_eta_minutes,
                                "delay_minutes": realtime["delay_minutes"],
                                "has_realtime": realtime["has_realtime"],
                                "train_position": realtime["train_position"],
                                "stops": stops,
                            }
                        )
            except Exception:
                return []
            return departures

        def collect_departures(hub_ids, max_workers, wait_timeout):
            rows = []
            executor = ThreadPoolExecutor(max_workers=max_workers)
            futures = [executor.submit(query_hub, hub) for hub in hub_ids]
            try:
                for future in as_completed(futures, timeout=wait_timeout):
                    rows.extend(future.result())
            except FuturesTimeoutError:
                pass
            finally:
                for future in futures:
                    future.cancel()
                executor.shutdown(wait=False, cancel_futures=True)
            return rows

        all_departures = collect_departures(
            hubs,
            max_workers=8 if fast_mode else 12,
            wait_timeout=8 if fast_mode else 20,
        )

        # If fast mode returned nothing, retry once with broader coverage.
        if fast_mode and not all_departures:
            expanded_hubs = [
                hub for hub in BOARD_HUBS
                if hub in available_station_ids and str(hub) != str(station_id)
            ]
            all_departures = collect_departures(
                expanded_hubs,
                max_workers=10,
                wait_timeout=14,
            )

        deduped = {}
        for row in all_departures:
            key = row.pop("unique_key")
            if not row.get("time"):
                continue
            if key not in deduped:
                deduped[key] = row
                continue

            # Keep the row with the richest stop list and available platform.
            existing = deduped[key]
            existing_stops = len(existing.get("stops") or [])
            candidate_stops = len(row.get("stops") or [])
            if candidate_stops > existing_stops:
                deduped[key] = row
            elif not existing.get("platform") and row.get("platform"):
                deduped[key] = row

        rows = list(deduped.values())
        rows.sort(key=lambda x: x["time"])
        _BOARD_CACHE[cache_key] = rows
        return jsonify(rows)
    except Exception as exc:
        app.logger.exception("Error in station board query")
        return jsonify({"error": str(exc)}), 500


if __name__ == "__main__":
    print("Server running at: http://127.0.0.1:5000")
    app.run(debug=True, port=5000)
