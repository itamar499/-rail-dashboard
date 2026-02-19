from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
from datetime import datetime, timedelta
import time

from flask import Flask, jsonify, request, send_from_directory
from israelrailapi import TrainSchedule
import israelrailapi.schedule as rail_schedule
import israelrailapi.train_station as rail_station

app = Flask(__name__)

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


def _normalize_date(value):
    if _is_valid_date(value):
        return value
    return datetime.now().strftime("%Y-%m-%d")


def _normalize_time(value):
    if not value:
        return datetime.now().strftime("%H:%M")
    candidate = value[:5]
    if _is_valid_time(candidate):
        return candidate
    return datetime.now().strftime("%H:%M")


def _parse_iso_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
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
                "stops": stops,
            }
        )

    return {
        "start_time": route.start_time,
        "end_time": route.end_time,
        "trains": trains,
    }


def _build_train_stops(train):
    stops = []
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

    # Deduplicate neighboring stations after src/dst patching.
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
    now = datetime.now()
    date_str = _normalize_date(request.args.get("date", now.strftime("%Y-%m-%d")))
    time_str = _normalize_time(request.args.get("time", now.strftime("%H:%M")))

    try:
        app.logger.info(
            "Querying route %s -> %s (%s %s)", from_id, to_id, date_str, time_str
        )
        results = TrainSchedule.query(from_id, to_id, date_str, time_str)
        return jsonify([route_to_dict(r) for r in results])
    except Exception:
        try:
            results = TrainSchedule.query(
                STATION_MAP.get(from_id, from_id),
                STATION_MAP.get(to_id, to_id),
                date_str,
                time_str,
            )
            return jsonify([route_to_dict(r) for r in results])
        except Exception as exc:
            app.logger.exception("Error fetching route schedules")
            return jsonify({"error": str(exc), "details": "Error fetching schedules"}), 500


@app.route("/api/station-board/<station_id>")
def get_station_board(station_id):
    try:
        now = datetime.now()
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
            hubs = hubs[:20]

        def query_hub(hub):
            departures = []
            try:
                results = TrainSchedule.query(station_id, hub, date_str, time_str)
                for route in results:
                    for train in route.trains:
                        found_at_station = False
                        for stop in train.data.get("stopStations", []):
                            if str(stop.get("stationId")) != str(station_id):
                                continue
                            found_at_station = True

                            train_num = train.data.get("trainNumber")
                            departure_time = stop.get("departureTime") or stop.get("arrivalTime")
                            departure_dt = _parse_iso_datetime(departure_time)

                            if only_upcoming and departure_dt:
                                if departure_dt < now or departure_dt > max_time:
                                    continue

                            eta_minutes = None
                            if departure_dt:
                                eta_minutes = int((departure_dt - now).total_seconds() // 60)
                            stops = _build_train_stops(train)
                            line_name = _line_name_from_stops(
                                stops,
                                get_station_name(train.src),
                                get_station_name(train.dst),
                            )

                            departures.append(
                                {
                                    "unique_key": _board_key(
                                        stops[0]["name"] if stops else get_station_name(train.src),
                                        stops[-1]["name"] if stops else get_station_name(train.dst),
                                        departure_time,
                                        train_num,
                                    ),
                                    "train_number": train_num,
                                    "src": get_station_name(train.src),
                                    "dest": get_station_name(train.dst),
                                    "line_name": line_name,
                                    "time": departure_time,
                                    "platform": stop.get("platform"),
                                    "eta_minutes": eta_minutes,
                                    "stops": stops,
                                }
                            )

                        # In many API responses, source station is not listed in stopStations.
                        if not found_at_station and str(train.src) == str(station_id):
                            train_num = train.data.get("trainNumber")
                            departure_time = train.departure
                            departure_dt = _parse_iso_datetime(departure_time)

                            if only_upcoming and departure_dt:
                                if departure_dt < now or departure_dt > max_time:
                                    continue

                            eta_minutes = None
                            if departure_dt:
                                eta_minutes = int((departure_dt - now).total_seconds() // 60)
                            stops = _build_train_stops(train)
                            line_name = _line_name_from_stops(
                                stops,
                                get_station_name(train.src),
                                get_station_name(train.dst),
                            )

                            departures.append(
                                {
                                    "unique_key": _board_key(
                                        stops[0]["name"] if stops else get_station_name(train.src),
                                        stops[-1]["name"] if stops else get_station_name(train.dst),
                                        departure_time,
                                        train_num,
                                    ),
                                    "train_number": train_num,
                                    "src": get_station_name(train.src),
                                    "dest": get_station_name(train.dst),
                                    "line_name": line_name,
                                    "time": departure_time,
                                    "platform": train.platform,
                                    "eta_minutes": eta_minutes,
                                    "stops": stops,
                                }
                            )
            except Exception:
                return []
            return departures

        all_departures = []
        executor = ThreadPoolExecutor(max_workers=10 if fast_mode else 12)
        futures = [executor.submit(query_hub, hub) for hub in hubs]
        try:
            for future in as_completed(futures, timeout=12 if fast_mode else 20):
                all_departures.extend(future.result())
        except FuturesTimeoutError:
            pass
        finally:
            for future in futures:
                future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)

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




