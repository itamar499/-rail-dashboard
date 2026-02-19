from datetime import datetime

from israelrailapi import TrainSchedule


def test_api():
    print("Connecting to Israel Rail API...")

    try:
        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H:%M")

        src = "Netivot"
        dst = "Tel Aviv Savidor Center"
        print(f"Testing route: {src} -> {dst} on {date_str} at {time_str}")
        results = TrainSchedule.query(src, dst, date_str, time_str)

        if results:
            print(f"Found {len(results)} routes.")
            print("\nNearest route:")
            route = results[0]
            print(f"Departure: {route.start_time}")
            print(f"Arrival: {route.end_time}")
            for i, train in enumerate(route.trains):
                print(f"  Train {i + 1}: Platform {train.platform}, departs {train.departure}")
                stops = train.data.get("stopStations", [])
                print(f"    Stops on route ({len(stops)}):")
                for s in stops:
                    print(f"      - {s.get('stationNameHeb') or s.get('stationNameEng')} ({s.get('arrivalTime')})")
        else:
            print("No routes were found for this query.")

    except Exception as exc:
        print(f"Error while connecting to API: {exc}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    test_api()
