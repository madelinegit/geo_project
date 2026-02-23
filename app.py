from flask import Flask, render_template, request, jsonify
import sqlite3
import requests
from ortools.constraint_solver import pywrapcp, routing_enums_pb2

app = Flask(__name__)

DB_PATH = "data/properties.db"

DEFAULT_START = {
    "name": "Tahoe Getaways Office",
    "lat": 39.3279,
    "lng": -120.1833,
}

CHECKIN_DEADLINE_HHMM = "16:00"  # 4PM COMPLETION deadline (finish service by 4PM)


# ---------------- TIME HELPERS ---------------- #

def hhmm_to_minutes(hhmm: str) -> int:
    parts = (hhmm or "").strip().split(":")
    if len(parts) != 2:
        raise ValueError("Invalid time format. Use HH:MM.")
    hh = int(parts[0])
    mm = int(parts[1])
    if hh < 0 or hh > 23 or mm < 0 or mm > 59:
        raise ValueError("Invalid time format. Use HH:MM.")
    return hh * 60 + mm


def minutes_to_hhmm(m: int) -> str:
    m = max(0, int(m))
    return f"{(m // 60) % 24:02d}:{m % 60:02d}"


# ---------------- HOME ---------------- #

@app.route("/")
def home():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT "Property Name", "Unit Address", Latitude, Longitude
        FROM properties
        WHERE Latitude IS NOT NULL AND Longitude IS NOT NULL
        """
    )

    rows = cursor.fetchall()
    conn.close()

    properties = []
    for r in rows:
        properties.append(
            {
                "name": r[0],
                "address": r[1],
                "lat": float(r[2]),
                "lng": float(r[3]),
            }
        )

    return render_template(
        "map.html",
        properties=properties,
        property_count=len(properties),
        default_start=DEFAULT_START,
    )


# ---------------- ORTOOLS SOLVER ---------------- #

def _solve_route(
    duration_matrix,
    service_times_sec,
    checkin_flags,
    deadline_offset_sec=None,
    hard_deadline=False,
    soft_deadline_penalty=False,
):
    """
    Solves a single-vehicle TSP-like route.

    duration_matrix: seconds travel time between nodes (size x size)
    service_times_sec: seconds service time at each node (node 0 start = 0)
    checkin_flags: bool list aligned to nodes (node 0 False)
    deadline_offset_sec: seconds since route start corresponding to 4PM (deadline - start_time)
    hard_deadline: if True, enforce check-ins must FINISH by deadline (hard constraint)
    soft_deadline_penalty: if True, add penalty for check-ins finishing after deadline (soft objective)

    Returns:
      ordered_nodes (list of node ids, includes 0 at start and includes end which often maps to 0)
      arrival_times_sec (aligned cumul arrival at each node position in ordered_nodes)
    """
    size = len(duration_matrix)
    manager = pywrapcp.RoutingIndexManager(size, 1, 0)
    routing = pywrapcp.RoutingModel(manager)

    def time_callback(from_index, to_index):
        from_node = manager.IndexToNode(from_index)
        to_node = manager.IndexToNode(to_index)
        travel = duration_matrix[from_node][to_node] or 0
        service = service_times_sec[from_node] or 0  # service at FROM node
        return int(travel + service)

    transit_cb = routing.RegisterTransitCallback(time_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_cb)

    horizon = 24 * 60 * 60
    routing.AddDimension(
        transit_cb,
        horizon,   # slack/waiting allowed
        horizon,   # max cumul
        True,      # start cumul at 0
        "Time",
    )
    time_dim = routing.GetDimensionOrDie("Time")

    # ---- Deadline handling (finish-by-4PM) ----
    # Completion: finish_time = arrival_time + service_here <= deadline_offset
    # So arrival_time <= deadline_offset - service_here
    if deadline_offset_sec is not None and deadline_offset_sec >= 0:
        # penalty coefficient: MUST be large vs travel seconds to push check-ins earlier
        # (travel objective ~ seconds; penalty coefficient * seconds late)
        PENALTY_PER_SEC_LATE = 5000

        for node_idx in range(1, size):
            if not bool(checkin_flags[node_idx]):
                continue

            idx = manager.NodeToIndex(node_idx)

            service_here = int(service_times_sec[node_idx] or 0)
            latest_arrival = int(deadline_offset_sec - service_here)
            if latest_arrival < 0:
                latest_arrival = 0

            if hard_deadline:
                # hard constraint: must arrive by latest_arrival
                time_dim.CumulVar(idx).SetRange(0, latest_arrival)

            if soft_deadline_penalty:
                # soft constraint: penalize arriving after latest_arrival
                # NOTE: this is arrival-based but represents completion because we subtract service_here above
                time_dim.SetCumulVarSoftUpperBound(idx, latest_arrival, PENALTY_PER_SEC_LATE)

    search_parameters = pywrapcp.DefaultRoutingSearchParameters()
    search_parameters.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    search_parameters.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    search_parameters.time_limit.FromSeconds(3)

    solution = routing.SolveWithParameters(search_parameters)
    if not solution:
        return None, None

    index = routing.Start(0)
    ordered_nodes = []
    arrival_times_sec = []

    while True:
        node = manager.IndexToNode(index)
        ordered_nodes.append(node)
        arrival_times_sec.append(solution.Value(time_dim.CumulVar(index)))

        if routing.IsEnd(index):
            break

        index = solution.Value(routing.NextVar(index))

    return ordered_nodes, arrival_times_sec


# ---------------- OPTIMIZE ---------------- #

@app.route("/optimize", methods=["POST"])
def optimize():
    data = request.json or {}
    stops = data.get("stops", [])
    start = data.get("start") or DEFAULT_START
    start_time_hhmm = (data.get("startTime") or "09:00").strip()

    if not stops:
        return jsonify({"error": "No stops provided"}), 400

    try:
        start_minutes = hhmm_to_minutes(start_time_hhmm)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    deadline_minutes = hhmm_to_minutes(CHECKIN_DEADLINE_HHMM)

    # Make sure coords are numeric (avoid weird frontend nulls)
    try:
        start = {
            "name": start.get("name"),
            "lat": float(start.get("lat")),
            "lng": float(start.get("lng")),
        }
    except Exception:
        return jsonify({"error": "Start location must have valid lat/lng."}), 400

    cleaned_stops = []
    for s in stops:
        try:
            cleaned_stops.append(
                {
                    "name": s.get("name"),
                    "lat": float(s.get("lat")),
                    "lng": float(s.get("lng")),
                    "arrival": bool(s.get("arrival", False)),
                    "serviceMinutes": int(s.get("serviceMinutes", 60)),
                }
            )
        except Exception:
            # skip any stop missing coords
            continue

    if not cleaned_stops:
        return jsonify({"error": "No valid stops (missing lat/lng)."}), 400

    all_locations = [start] + cleaned_stops

    # OSRM MATRIX
    coords = ";".join(f"{float(s['lng'])},{float(s['lat'])}" for s in all_locations)
    matrix_url = f"http://router.project-osrm.org/table/v1/driving/{coords}?annotations=duration"

    resp = requests.get(matrix_url, timeout=30)
    if resp.status_code != 200:
        return jsonify({"error": "OSRM matrix request failed"}), 500

    duration_matrix = resp.json().get("durations")
    if not duration_matrix:
        return jsonify({"error": "Invalid matrix response"}), 500

    # SERVICE TIMES (seconds); node 0 is start
    service_times_sec = [0]
    for s in cleaned_stops:
        m = max(0, int(s.get("serviceMinutes", 60)))
        service_times_sec.append(m * 60)

    # CHECK-IN FLAGS aligned to nodes
    checkin_flags = [False] + [bool(s.get("arrival", False)) for s in cleaned_stops]

    enforce_deadline = start_minutes < deadline_minutes
    deadline_offset_sec = (deadline_minutes - start_minutes) * 60 if enforce_deadline else None

    # Option 3 behavior:
    # 1) Try HARD completion-by-4PM for check-ins.
    # 2) If infeasible, fallback to SOFT penalties (minimize lateness as much as possible).
    ordered_nodes, arrival_times_sec = None, None
    used_deadline_constraints = False
    used_soft_penalties = False

    if enforce_deadline:
        ordered_nodes, arrival_times_sec = _solve_route(
            duration_matrix=duration_matrix,
            service_times_sec=service_times_sec,
            checkin_flags=checkin_flags,
            deadline_offset_sec=deadline_offset_sec,
            hard_deadline=True,
            soft_deadline_penalty=False,
        )
        if ordered_nodes is not None:
            used_deadline_constraints = True

    if ordered_nodes is None:
        # soft penalty solve (prioritizes check-ins earlier, even if not all can finish by 4)
        ordered_nodes, arrival_times_sec = _solve_route(
            duration_matrix=duration_matrix,
            service_times_sec=service_times_sec,
            checkin_flags=checkin_flags,
            deadline_offset_sec=deadline_offset_sec if enforce_deadline else None,
            hard_deadline=False,
            soft_deadline_penalty=True,
        )
        if ordered_nodes is not None:
            used_soft_penalties = True

    if ordered_nodes is None:
        # final fallback: no deadline logic at all (should be rare)
        ordered_nodes, arrival_times_sec = _solve_route(
            duration_matrix=duration_matrix,
            service_times_sec=service_times_sec,
            checkin_flags=checkin_flags,
            deadline_offset_sec=None,
            hard_deadline=False,
            soft_deadline_penalty=False,
        )
        if ordered_nodes is None:
            return jsonify({"error": "No solution found"}), 500

    # Map node -> arrival seconds (first occurrence)
    node_arrival_sec = {}
    for pos, node in enumerate(ordered_nodes):
        if node not in node_arrival_sec:
            node_arrival_sec[node] = arrival_times_sec[pos]

    # Extract stop nodes (exclude the first start, and exclude any 0 that appears at end)
    ordered_stop_nodes = [n for n in ordered_nodes[1:] if n != 0]

    ordered_stops = [all_locations[n] for n in ordered_stop_nodes]

    # OSRM ROUTE GEOMETRY
    coords_final = ";".join(f"{float(s['lng'])},{float(s['lat'])}" for s in [start] + ordered_stops)
    route_url = f"http://router.project-osrm.org/route/v1/driving/{coords_final}?overview=full&geometries=geojson"
    route_resp = requests.get(route_url, timeout=30)
    if route_resp.status_code != 200:
        return jsonify({"error": "OSRM route request failed"}), 500

    route_data = route_resp.json().get("routes", [{}])[0]
    if not route_data:
        return jsonify({"error": "Invalid OSRM route response"}), 500

    driving_duration = float(route_data.get("duration", 0.0))
    service_duration = sum(int(s.get("serviceMinutes", 60)) * 60 for s in ordered_stops)
    total_duration = driving_duration + service_duration

    # BUILD SCHEDULE (completion-by-4PM lateness)
    schedule = []
    late_checkins = []

    for node in ordered_stop_nodes:
        stop = all_locations[node]

        eta_minutes = start_minutes + int(node_arrival_sec.get(node, 0) // 60)
        service_min = int(stop.get("serviceMinutes", 60))
        finish_minutes = eta_minutes + service_min

        is_checkin = bool(stop.get("arrival", False))
        is_late = False
        if is_checkin and finish_minutes > deadline_minutes:
            is_late = True
            late_checkins.append(stop.get("name"))

        schedule.append(
            {
                "name": stop.get("name"),
                "arrival": is_checkin,
                "late": is_late,
                "serviceMinutes": service_min,
                "eta": minutes_to_hhmm(eta_minutes),
                "eta_minutes": eta_minutes,
                "lat": float(stop.get("lat")),
                "lng": float(stop.get("lng")),
            }
        )

    return jsonify(
        {
            "distance": route_data.get("distance", 0.0),
            "total_duration": total_duration,
            "driving_duration": driving_duration,
            "service_duration": service_duration,
            "geometry": route_data.get("geometry"),
            "ordered_stops": ordered_stops,  # keep for compatibility
            "start_time": start_time_hhmm,
            "checkin_deadline": CHECKIN_DEADLINE_HHMM,
            "schedule": schedule,
            "late_checkins": late_checkins,
            "deadline_constraints_used": used_deadline_constraints,
            "soft_penalties_used": used_soft_penalties,
        }
    )


if __name__ == "__main__":
    app.run(debug=True)