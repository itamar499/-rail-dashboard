from __future__ import annotations

import csv
import json
import os
import tempfile
import zipfile
from datetime import UTC, datetime

import requests


GTFS_URL = "https://gtfs.mot.gov.il/gtfsfiles/israel-public-transportation.zip"
RAIL_AGENCY_ID = "2"
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_PATH = os.path.join(ROOT_DIR, "assets", "rail_gtfs_compact.json")


def _rows(zip_file: zipfile.ZipFile, name: str):
    with zip_file.open(name) as fp:
        for row in csv.DictReader((line.decode("utf-8-sig") for line in fp)):
            yield row


def _download_gtfs_zip() -> str:
    fd, path = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    try:
        with requests.get(
            GTFS_URL,
            timeout=(5, 180),
            stream=True,
            headers={"User-Agent": "Mozilla/5.0"},
        ) as response:
            response.raise_for_status()
            with open(path, "wb") as fp:
                for chunk in response.iter_content(1024 * 1024):
                    if chunk:
                        fp.write(chunk)
        return path
    except Exception:
        try:
            os.remove(path)
        except OSError:
            pass
        raise


def _hhmmss_to_seconds(value: str) -> int:
    hour, minute, second = [int(part) for part in value.split(":")]
    return (hour * 3600) + (minute * 60) + second


def _iter_stop_times_fast(zip_file: zipfile.ZipFile, rail_trip_ids: set[str]):
    # stop_times.txt is the huge file in the national GTFS feed. It has no
    # quoted commas in the fields we need, so byte splitting is much faster
    # than csv.DictReader here.
    with zip_file.open("stop_times.txt") as fp:
        header = fp.readline().decode("utf-8-sig").strip().split(",")
        trip_idx = header.index("trip_id")
        arr_idx = header.index("arrival_time")
        dep_idx = header.index("departure_time")
        stop_idx = header.index("stop_id")
        for raw_line in fp:
            parts = raw_line.rstrip(b"\r\n").split(b",")
            if len(parts) <= stop_idx:
                continue
            trip_id = parts[trip_idx].decode("utf-8")
            if trip_id not in rail_trip_ids:
                continue
            yield (
                trip_id,
                parts[stop_idx].decode("utf-8"),
                parts[arr_idx].decode("utf-8"),
                parts[dep_idx].decode("utf-8"),
            )


def _station_name_variants(name: str) -> set[str]:
    clean = " ".join(str(name or "").replace("-", " ").split())
    variants = {clean}
    if "<->" in clean:
        variants.update(part.strip() for part in clean.split("<->") if part.strip())
    if "-" in str(name or ""):
        variants.update(part.strip() for part in str(name).split("-") if part.strip())
    return {variant for variant in variants if variant}


def build_compact(zip_path: str) -> dict:
    with zipfile.ZipFile(zip_path) as zf:
        rail_route_ids = {
            row["route_id"]
            for row in _rows(zf, "routes.txt")
            if row.get("agency_id") == RAIL_AGENCY_ID
        }

        rail_trips = {}
        service_ids = set()
        for row in _rows(zf, "trips.txt"):
            if row.get("route_id") not in rail_route_ids:
                continue
            trip_id = row["trip_id"]
            rail_trips[trip_id] = {
                "id": trip_id,
                "svc": row.get("service_id"),
                "route": row.get("route_id"),
                "stops": [],
            }
            service_ids.add(row.get("service_id"))

        stops_by_id = {}
        for row in _rows(zf, "stops.txt"):
            stops_by_id[row["stop_id"]] = {
                "name": row.get("stop_name") or row["stop_id"],
                "code": row.get("stop_code"),
            }

        rail_trip_ids = set(rail_trips)
        for trip_id, stop_id, arrival_time, departure_time in _iter_stop_times_fast(zf, rail_trip_ids):
            trip = rail_trips[trip_id]
            trip["stops"].append(
                [
                    stop_id,
                    _hhmmss_to_seconds(arrival_time),
                    _hhmmss_to_seconds(departure_time),
                ]
            )

        rail_stop_ids = set()
        for trip in rail_trips.values():
            trip["stops"].sort(key=lambda stop: stop[1])
            rail_stop_ids.update(stop[0] for stop in trip["stops"])

        calendar = {
            row["service_id"]: {
                "start": row.get("start_date"),
                "end": row.get("end_date"),
                "days": [
                    int(row.get("monday", 0)),
                    int(row.get("tuesday", 0)),
                    int(row.get("wednesday", 0)),
                    int(row.get("thursday", 0)),
                    int(row.get("friday", 0)),
                    int(row.get("saturday", 0)),
                    int(row.get("sunday", 0)),
                ],
            }
            for row in _rows(zf, "calendar.txt")
            if row.get("service_id") in service_ids
        }

    rail_stops = {sid: stops_by_id[sid] for sid in rail_stop_ids if sid in stops_by_id}
    station_map = {}
    for sid, stop in rail_stops.items():
        for variant in _station_name_variants(stop["name"]):
            station_map.setdefault(variant, sid)
        if stop.get("code"):
            station_map.setdefault(str(stop["code"]), sid)

    return {
        "generated_from": GTFS_URL,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "stops": rail_stops,
        "station_map": station_map,
        "calendar": calendar,
        "trips": list(rail_trips.values()),
    }


def main() -> None:
    zip_path = _download_gtfs_zip()
    try:
        payload = build_compact(zip_path)
    finally:
        os.remove(zip_path)

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, separators=(",", ":"))
    print(
        f"Wrote {OUTPUT_PATH}: "
        f"{len(payload['stops'])} stops, {len(payload['trips'])} trips, "
        f"{len(payload['calendar'])} services"
    )


if __name__ == "__main__":
    main()
