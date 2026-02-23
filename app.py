from flask import Flask, render_template, request, jsonify
import sqlite3
import requests
from datetime import datetime
from ortools.constraint_solver import pywrapcp, routing_enums_pb2

app = Flask(__name__)

DB_PATH = "data/properties.db"

DEFAULT_START = {
    "name": "Tahoe Getaways Office",
    "lat": 39.3279,
    "lng": -120.1833
}

CHECKIN_DEADLINE_HHMM = "16:00"  # 4PM hard deadline


def hhmm_to_minutes(hhmm: str) -> int:
    try:
        parts = hhmm.strip().split(":")
        return int(parts[0]) * 60 + int(parts[1])
    except Exception:
        raise ValueError("Invalid time format. Use HH:MM (24-hour).")


def minutes_to_hhmm(m: int) -> str:
    m = max(0, int(m))
    hh = (m // 60) % 24
    mm = m % 60
    return f"{hh:02d}:{mm:02d}"


@app.route("/")
def home():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT "Property Name", "Unit Address", Latitude, Longitude
        FROM properties
        WHERE Latitude IS NOT NULL AND Longitude IS NOT NULL
    """)

    rows = cursor.fetchall()
    conn.close()

    properties = []
    for r in rows:
        properties.append({
            "name": r[0],
            "address": r[1],
            "lat": r[2],
            "lng": r[3]
        })

    return render_template(
        "map.html",
        properties=properties,
        property_count=len(properties),
        default_start=DEFAULT_START
    )


@app.route("/optimize", methods=["POST"])
def optimize():
    data = request.json or {}
    stops = data.get("stops", [])
    start = data.get("start") or DEFAULT_START
    start_time_hhmm = (data.get("startTime") or "09:00").strip()  # default 9AM

    if not stops:
        return jsonify({"error": "No stops provided"}), 400

    # Validate start time
    try:
        start_minutes = hhmm_to_minutes(start_time_hhmm)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    deadline_minutes = hhmm_to_minutes(CHECKIN_DEADLINE_HHMM)
    if start_minutes >= deadline_minutes:
        return jsonify({"error": f"Start time must be before {CHECKIN_DEADLINE_HHMM} for check-in deadline logic."}), 400

    # Combine start + stops
    all_locations = [start] + stops

    # Build coordinate string for OSRM table API (travel time matrix)
    coords = ";".join(
        f"{float(s['lng'])},{float(s['lat'])}"
        for s in all_locations
    )

    matrix_url = f"http://router.project-osrm.org/table/v1/driving/{coords}?annotations=duration"
    resp = requests.get(matrix_url, timeout=30)

    if resp.status_code != 200:
        return jsonify({"error": "OSRM matrix request failed"}), 500

    matrix_data = resp.json()
    duration_matrix = matrix_data.get("durations")

    if not duration_matrix or any(row is None for row in duration_matrix):
        return jsonify({"error": "OSRM returned an invalid duration matrix"}), 500

    size = len(duration_matrix)

    # Service times per node (seconds). Node 0 is start.
    service_times_sec = [0]
    for stop in stops:
        minutes = int(stop.get("serviceMinutes", 60))
        service_times_sec.append(max(0, minutes) * 60)

    # OR-Tools
    manager = pywrapcp.RoutingIndexManager(size, 1, 0)
    routing = pywrapcp.RoutingModel(manager)

    def time_callback(from_index, to_index):
        from_node = manager.IndexToNode(from_index)
        to_node = manager.IndexToNode(to_index)
        travel_time = duration_matrix[from_node][to_node] or 0
        service_time = service_times_sec[from_node]
        return int(travel_time + service_time)

    transit_cb = routing.RegisterTransitCallback(time_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_cb)

    # Time dimension: allow up to 24 hours (seconds)
    horizon = 24 * 60 * 60
    routing.AddDimension(
        transit_cb,
        0,          # no slack (keeps it strict)
        horizon,
        True,       # force start cumul = 0
        "Time"
    )
    time_dim = routing.GetDimensionOrDie("Time")

    # Apply hard deadline for check-ins: check-in must be *arrived at* by 4PM local.
    # Since the model's time starts at 0, convert to offset seconds:
    checkin_deadline_offset_sec = (deadline_minutes - start_minutes) * 60

    # For every stop node (1..n), if arrival=True, restrict its cumul upper bound
    for idx_in_all in range(1, size):
        stop_obj = all_locations[idx_in_all]
        is_checkin = bool(stop_obj.get("arrival", False))
        if is_checkin:
            node_index = manager.NodeToIndex(idx_in_all)
            # Hard window: must arrive by deadline
            time_dim.CumulVar(node_index).SetRange(0, checkin_deadline_offset_sec)

    # Search params
    search_parameters = pywrapcp.DefaultRoutingSearchParameters()
    search_parameters.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    search_parameters.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    search_parameters.time_limit.FromSeconds(3)

    solution = routing.SolveWithParameters(search_parameters)
    if not solution:
        return jsonify({"error": "No feasible solution (check-ins may be impossible to complete by 4PM)."}), 500

    # Extract order + per-node arrival times (seconds since start)
    index = routing.Start(0)
    ordered_nodes = []  # nodes in all_locations indices
    arrival_times_sec = []

    while not routing.IsEnd(index):
        node = manager.IndexToNode(index)
        ordered_nodes.append(node)
        arrival_times_sec.append(solution.Value(time_dim.CumulVar(index)))
        index = solution.Value(routing.NextVar(index))

    # ordered_nodes includes start at 0 as first
    # Build ordered stops list excluding start
    ordered_stop_nodes = ordered_nodes[1:]
    ordered_stops = [all_locations[n] for n in ordered_stop_nodes]

    # Build OSRM route geometry for display (start + ordered stops)
    ordered_locations = [start] + ordered_stops
    coords_final = ";".join(
        f"{float(s['lng'])},{float(s['lat'])}"
        for s in ordered_locations
    )

    route_url = f"http://router.project-osrm.org/route/v1/driving/{coords_final}?overview=full&geometries=geojson"
    route_resp = requests.get(route_url, timeout=30)
    if route_resp.status_code != 200:
        return jsonify({"error": "OSRM final route request failed"}), 500

    route_data = route_resp.json()["routes"][0]

    driving_duration = float(route_data["duration"])  # seconds
    service_duration = sum(int(s.get("serviceMinutes", 60)) * 60 for s in ordered_stops)
    total_duration = driving_duration + service_duration

    # Build schedule per stop: ETA (arrival) in local HH:MM using start_time
    # We want ETAs for the ordered route list (excluding start).
    # We need arrival_times for those nodes in route order.
    # arrival_times_sec list aligns with ordered_nodes (includes start at index 0).
    schedule = []
    for i, node in enumerate(ordered_stop_nodes, start=1):
        stop = all_locations[node]
        eta_minutes = start_minutes + int(arrival_times_sec[i] // 60)
        schedule.append({
            "name": stop.get("name"),
            "arrival": bool(stop.get("arrival", False)),
            "serviceMinutes": int(stop.get("serviceMinutes", 60)),
            "eta": minutes_to_hhmm(eta_minutes),
            "eta_minutes": eta_minutes
        })

    return jsonify({
        "distance": route_data["distance"],
        "total_duration": total_duration,
        "driving_duration": driving_duration,
        "service_duration": service_duration,
        "geometry": route_data["geometry"],
        "ordered_stops": ordered_stops,
        "start_time": start_time_hhmm,
        "checkin_deadline": CHECKIN_DEADLINE_HHMM,
        "schedule": schedule
    })


if __name__ == "__main__":
    app.run(debug=True)