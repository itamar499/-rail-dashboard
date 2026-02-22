from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
from datetime import datetime, timedelta
import time
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, request, send_from_directory
from israelrailapi import TrainSchedule
import israelrailapi.api as rail_api
import israelrailapi.schedule as rail_schedule
import israelrailapi.train_station as rail_station

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


def _requests_post_with_timeout(*args, **kwargs):
    kwargs.setdefault("timeout", (4, 10))
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

    try:
        app.logger.info(
            "Querying route %s -> %s (%s %s)", from_id, to_id, date_str, time_str
        )
        results = TrainSchedule.query(from_id, to_id, date_str, time_str)
        results = _filter_routes_from_request_time(results, date_str, time_str)
        return jsonify([route_to_dict(r) for r in results])
    except Exception:
        try:
            results = TrainSchedule.query(
                STATION_MAP.get(from_id, from_id),
                STATION_MAP.get(to_id, to_id),
                date_str,
                time_str,
            )
            results = _filter_routes_from_request_time(results, date_str, time_str)
            return jsonify([route_to_dict(r) for r in results])
        except Exception as exc:
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





