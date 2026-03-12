from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, session
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
import sqlite3
import requests
import json
import os
import secrets
from datetime import datetime, timedelta
from ortools.constraint_solver import pywrapcp, routing_enums_pb2
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail

load_dotenv()  # reads .env from project root into os.environ

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-this-in-production")

# ── Flask-Login setup ──────────────────────────────────────────────
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Please log in to access Tahoe Dispatch."
login_manager.login_message_category = "info"

# ── Config ────────────────────────────────────────────────────────
DB_PATH = "data/properties.db"
SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY", "")
FROM_EMAIL = "operations@tahoegetaways.com"
APP_BASE_URL = os.environ.get("APP_BASE_URL", "http://localhost:5000")

DEFAULT_START = {
    "name": "Tahoe Getaways Office",
    "lat": 39.3279,
    "lng": -120.1833,
}

CHECKIN_DEADLINE_HHMM = "16:00"


# ══════════════════════════════════════════════════════════════════
#  DB HELPERS
# ══════════════════════════════════════════════════════════════════

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        role TEXT DEFAULT 'user',
        password_hash TEXT NOT NULL,
        is_active INTEGER DEFAULT 1,
        reset_token TEXT,
        reset_token_expires TEXT,
        created_at TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS saved_routes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        route_date TEXT,
        stops_json TEXT,
        total_duration REAL,
        driving_duration REAL,
        service_duration REAL,
        distance REAL,
        created_by INTEGER,
        last_edited_by INTEGER,
        created_at TEXT,
        updated_at TEXT
    )""")
    # Seed admin user if none exists
    from werkzeug.security import generate_password_hash
    from datetime import datetime
    existing = conn.execute("SELECT id FROM users WHERE role='admin'").fetchone()
    if not existing:
        admin_password = os.environ.get("ADMIN_PASSWORD", "ChangeMe123!")
        conn.execute(
            "INSERT INTO users (email, name, role, password_hash, is_active, created_at) VALUES (?,?,?,?,1,?)",
            ("operations@tahoegetaways.com", "Admin", "admin",
             generate_password_hash(admin_password), datetime.utcnow().isoformat())
        )
    conn.commit()
    conn.close()

with app.app_context():
    init_db()


# ══════════════════════════════════════════════════════════════════
#  USER MODEL
# ══════════════════════════════════════════════════════════════════

class User(UserMixin):
    def __init__(self, id, email, name, role, is_active):
        self.id        = id
        self.email     = email
        self.name      = name
        self.role      = role          # "admin" or "user"
        self._active   = is_active

    @property
    def is_active(self):
        return bool(self._active)

    @property
    def is_admin(self):
        return self.role == "admin"


@login_manager.user_loader
def load_user(user_id):
    conn = get_db()
    row = conn.execute(
        "SELECT id, email, name, role, is_active FROM users WHERE id = ?",
        (user_id,)
    ).fetchone()
    conn.close()
    if row:
        return User(row["id"], row["email"], row["name"], row["role"], row["is_active"])
    return None


# ══════════════════════════════════════════════════════════════════
#  TIME HELPERS
# ══════════════════════════════════════════════════════════════════

def hhmm_to_minutes(hhmm: str) -> int:
    parts = (hhmm or "").strip().split(":")
    if len(parts) != 2:
        raise ValueError("Invalid time format. Use HH:MM.")
    hh, mm = int(parts[0]), int(parts[1])
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError("Invalid time format. Use HH:MM.")
    return hh * 60 + mm


def minutes_to_hhmm(m: int) -> str:
    m = max(0, int(m))
    return f"{(m // 60) % 24:02d}:{m % 60:02d}"


# ══════════════════════════════════════════════════════════════════
#  AUTH ROUTES
# ══════════════════════════════════════════════════════════════════

@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("home"))

    if request.method == "POST":
        email    = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        remember = bool(request.form.get("remember"))

        conn = get_db()
        row = conn.execute(
            "SELECT id, email, name, role, password_hash, is_active FROM users WHERE email = ?",
            (email,)
        ).fetchone()
        conn.close()

        if not row or not check_password_hash(row["password_hash"], password):
            flash("Invalid email or password.", "error")
            return render_template("login.html")

        if not row["is_active"]:
            flash("Your account has been deactivated. Contact operations@tahoegetaways.com.", "error")
            return render_template("login.html")

        user = User(row["id"], row["email"], row["name"], row["role"], row["is_active"])
        login_user(user, remember=remember)
        return redirect(request.args.get("next") or url_for("home"))

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("You've been logged out.", "info")
    return redirect(url_for("login"))


# ── Password reset ─────────────────────────────────────────────────

@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        conn  = get_db()
        row   = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()

        if row:
            token   = secrets.token_urlsafe(32)
            expires = (datetime.utcnow() + timedelta(hours=1)).isoformat()
            conn.execute(
                "UPDATE users SET reset_token = ?, reset_token_expires = ? WHERE id = ?",
                (token, expires, row["id"])
            )
            conn.commit()
            _send_reset_email(email, token)

        conn.close()
        # Always show success to avoid user enumeration
        flash("If that email is in our system, a reset link has been sent.", "info")
        return redirect(url_for("login"))

    return render_template("forgot_password.html")


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    conn = get_db()
    row  = conn.execute(
        "SELECT id, reset_token_expires FROM users WHERE reset_token = ?", (token,)
    ).fetchone()

    if not row:
        conn.close()
        flash("Invalid or expired reset link.", "error")
        return redirect(url_for("login"))

    if datetime.utcnow() > datetime.fromisoformat(row["reset_token_expires"]):
        conn.close()
        flash("This reset link has expired. Please request a new one.", "error")
        return redirect(url_for("forgot_password"))

    if request.method == "POST":
        password = request.form.get("password") or ""
        confirm  = request.form.get("confirm") or ""

        if len(password) < 8:
            flash("Password must be at least 8 characters.", "error")
            return render_template("reset_password.html", token=token)

        if password != confirm:
            flash("Passwords do not match.", "error")
            return render_template("reset_password.html", token=token)

        conn.execute(
            "UPDATE users SET password_hash = ?, reset_token = NULL, reset_token_expires = NULL WHERE id = ?",
            (generate_password_hash(password), row["id"])
        )
        conn.commit()
        conn.close()
        flash("Password updated. You can now log in.", "success")
        return redirect(url_for("login"))

    conn.close()
    return render_template("reset_password.html", token=token)


def _send_reset_email(to_email: str, token: str):
    reset_url = f"{APP_BASE_URL}/reset-password/{token}"
    message = Mail(
        from_email=FROM_EMAIL,
        to_emails=to_email,
        subject="Tahoe Dispatch — Password Reset",
        html_content=f"""
            <p>Hi,</p>
            <p>Click the link below to reset your Tahoe Dispatch password.
               This link expires in 1 hour.</p>
            <p><a href="{reset_url}">{reset_url}</a></p>
            <p>If you didn't request this, you can ignore this email.</p>
            <p>— Tahoe Getaways Operations</p>
        """
    )
    try:
        sg = SendGridAPIClient(SENDGRID_API_KEY)
        sg.send(message)
    except Exception as e:
        app.logger.error(f"SendGrid error: {e}")


# ══════════════════════════════════════════════════════════════════
#  ADMIN — USER MANAGEMENT
# ══════════════════════════════════════════════════════════════════

def admin_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            flash("Admin access required.", "error")
            return redirect(url_for("home"))
        return f(*args, **kwargs)
    return decorated


@app.route("/admin/users")
@login_required
@admin_required
def admin_users():
    conn  = get_db()
    users = conn.execute(
        "SELECT id, email, name, role, is_active, created_at FROM users ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return render_template("admin.html", users=users)


@app.route("/admin/users/add", methods=["POST"])
@login_required
@admin_required
def admin_add_user():
    email    = (request.form.get("email") or "").strip().lower()
    name     = (request.form.get("name") or "").strip()
    role     = request.form.get("role", "user")
    password = request.form.get("password") or ""

    if not email or not name or not password:
        flash("Email, name, and password are all required.", "error")
        return redirect(url_for("admin_users"))

    if role not in ("admin", "user"):
        role = "user"

    conn = get_db()
    existing = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if existing:
        flash(f"{email} already exists.", "error")
        conn.close()
        return redirect(url_for("admin_users"))

    conn.execute(
        "INSERT INTO users (email, name, role, password_hash, is_active, created_at) VALUES (?,?,?,?,1,?)",
        (email, name, role, generate_password_hash(password), datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()
    flash(f"User {name} ({email}) created.", "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/toggle", methods=["POST"])
@login_required
@admin_required
def admin_toggle_user(user_id):
    if user_id == current_user.id:
        flash("You cannot deactivate your own account.", "error")
        return redirect(url_for("admin_users"))

    conn = get_db()
    row  = conn.execute("SELECT is_active, name FROM users WHERE id = ?", (user_id,)).fetchone()
    if row:
        new_state = 0 if row["is_active"] else 1
        conn.execute("UPDATE users SET is_active = ? WHERE id = ?", (new_state, user_id))
        conn.commit()
        status = "activated" if new_state else "deactivated"
        flash(f"{row['name']} has been {status}.", "success")
    conn.close()
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/reset-password", methods=["POST"])
@login_required
@admin_required
def admin_reset_password(user_id):
    new_password = request.form.get("password") or ""
    if len(new_password) < 8:
        flash("Password must be at least 8 characters.", "error")
        return redirect(url_for("admin_users"))

    conn = get_db()
    conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (generate_password_hash(new_password), user_id)
    )
    conn.commit()
    conn.close()
    flash("Password updated.", "success")
    return redirect(url_for("admin_users"))


# ══════════════════════════════════════════════════════════════════
#  PORTFOLIO  (public — no login required)
# ══════════════════════════════════════════════════════════════════

@app.route("/portfolio")
def portfolio():
    conn   = get_db()
    cursor = conn.execute(
        'SELECT "Property Name", "Unit Address", Latitude, Longitude FROM properties '
        'WHERE Latitude IS NOT NULL AND Longitude IS NOT NULL '
        'ORDER BY "Property Name" ASC'
    )
    rows = cursor.fetchall()
    conn.close()

    properties = [
        {"name": r[0], "address": r[1], "lat": float(r[2]), "lng": float(r[3])}
        for r in rows
    ]
    return render_template("portfolio.html", properties=properties)


# ══════════════════════════════════════════════════════════════════
#  HOME
# ══════════════════════════════════════════════════════════════════

@app.route("/")
@login_required
def home():
    conn   = get_db()
    cursor = conn.execute(
        'SELECT "Property Name", "Unit Address", Latitude, Longitude FROM properties '
        'WHERE Latitude IS NOT NULL AND Longitude IS NOT NULL'
    )
    rows = cursor.fetchall()
    conn.close()

    properties = [
        {"name": r[0], "address": r[1], "lat": float(r[2]), "lng": float(r[3])}
        for r in rows
    ]

    return render_template(
        "map.html",
        properties=properties,
        property_count=len(properties),
        default_start=DEFAULT_START,
    )


# ══════════════════════════════════════════════════════════════════
#  SAVED ROUTES
# ══════════════════════════════════════════════════════════════════

@app.route("/routes")
@login_required
def saved_routes():
    conn   = get_db()
    routes = conn.execute(
        """SELECT r.id, r.name, r.route_date, r.created_at, r.updated_at,
                  r.total_duration, r.driving_duration, r.distance,
                  u.name AS created_by_name,
                  lu.name AS last_edited_by_name
           FROM saved_routes r
           JOIN users u  ON r.created_by  = u.id
           LEFT JOIN users lu ON r.last_edited_by = lu.id
           ORDER BY r.route_date DESC, r.updated_at DESC"""
    ).fetchall()
    conn.close()
    return render_template("routes.html", routes=routes)


@app.route("/routes/save", methods=["POST"])
@login_required
def save_route():
    data = request.json or {}

    name       = (data.get("name") or "").strip()
    route_date = (data.get("route_date") or "").strip()
    schedule   = data.get("schedule", [])
    stats      = data.get("stats", {})

    if not name:
        return jsonify({"error": "Route name is required."}), 400
    if not route_date:
        return jsonify({"error": "Route date is required."}), 400
    if not schedule:
        return jsonify({"error": "No stops to save."}), 400

    now = datetime.utcnow().isoformat()
    conn = get_db()
    cursor = conn.execute(
        """INSERT INTO saved_routes
           (name, route_date, stops_json, total_duration, driving_duration,
            service_duration, distance, created_by, last_edited_by, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            name,
            route_date,
            json.dumps(schedule),
            stats.get("total_duration", 0),
            stats.get("driving_duration", 0),
            stats.get("service_duration", 0),
            stats.get("distance", 0),
            current_user.id,
            current_user.id,
            now,
            now,
        )
    )
    conn.commit()
    route_id = cursor.lastrowid
    conn.close()
    return jsonify({"success": True, "id": route_id})


@app.route("/routes/<int:route_id>/update", methods=["POST"])
@login_required
def update_route(route_id):
    data     = request.json or {}
    schedule = data.get("schedule", [])
    stats    = data.get("stats", {})

    if not schedule:
        return jsonify({"error": "No stops to save."}), 400

    now  = datetime.utcnow().isoformat()
    conn = get_db()
    conn.execute(
        """UPDATE saved_routes SET
           stops_json = ?, total_duration = ?, driving_duration = ?,
           service_duration = ?, distance = ?,
           last_edited_by = ?, updated_at = ?
           WHERE id = ?""",
        (
            json.dumps(schedule),
            stats.get("total_duration", 0),
            stats.get("driving_duration", 0),
            stats.get("service_duration", 0),
            stats.get("distance", 0),
            current_user.id,
            now,
            route_id,
        )
    )
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route("/routes/<int:route_id>")
@login_required
def load_route(route_id):
    conn = get_db()
    row  = conn.execute(
        "SELECT * FROM saved_routes WHERE id = ?", (route_id,)
    ).fetchone()
    conn.close()

    if not row:
        flash("Route not found.", "error")
        return redirect(url_for("saved_routes"))

    schedule = json.loads(row["stops_json"])
    return jsonify({
        "id":               row["id"],
        "name":             row["name"],
        "route_date":       row["route_date"],
        "schedule":         schedule,
        "total_duration":   row["total_duration"],
        "driving_duration": row["driving_duration"],
        "service_duration": row["service_duration"],
        "distance":         row["distance"],
    })


@app.route("/routes/<int:route_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_route(route_id):
    conn = get_db()
    conn.execute("DELETE FROM saved_routes WHERE id = ?", (route_id,))
    conn.commit()
    conn.close()
    flash("Route deleted.", "success")
    return redirect(url_for("saved_routes"))


# ══════════════════════════════════════════════════════════════════
#  ORTOOLS SOLVER
# ══════════════════════════════════════════════════════════════════

def _solve_route(
    duration_matrix,
    service_times_sec,
    checkin_flags,
    deadline_offset_sec=None,
    hard_deadline=False,
    soft_deadline_penalty=False,
):
    size    = len(duration_matrix)
    manager = pywrapcp.RoutingIndexManager(size, 1, 0)
    routing = pywrapcp.RoutingModel(manager)

    def time_callback(from_index, to_index):
        from_node = manager.IndexToNode(from_index)
        to_node   = manager.IndexToNode(to_index)
        return int((duration_matrix[from_node][to_node] or 0) + (service_times_sec[from_node] or 0))

    transit_cb = routing.RegisterTransitCallback(time_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_cb)

    horizon = 24 * 60 * 60
    routing.AddDimension(transit_cb, horizon, horizon, True, "Time")
    time_dim = routing.GetDimensionOrDie("Time")

    if deadline_offset_sec is not None and deadline_offset_sec >= 0:
        PENALTY = 5000
        for node_idx in range(1, size):
            if not bool(checkin_flags[node_idx]):
                continue
            idx             = manager.NodeToIndex(node_idx)
            service_here    = int(service_times_sec[node_idx] or 0)
            latest_arrival  = int(deadline_offset_sec - service_here)
            if latest_arrival < 0:
                latest_arrival = 0
            if hard_deadline:
                time_dim.CumulVar(idx).SetRange(0, latest_arrival)
            if soft_deadline_penalty:
                time_dim.SetCumulVarSoftUpperBound(idx, latest_arrival, PENALTY)

    search_parameters = pywrapcp.DefaultRoutingSearchParameters()
    search_parameters.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    )
    search_parameters.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    )
    search_parameters.time_limit.FromSeconds(3)

    solution = routing.SolveWithParameters(search_parameters)
    if not solution:
        return None, None

    index              = routing.Start(0)
    ordered_nodes      = []
    arrival_times_sec  = []

    while True:
        node = manager.IndexToNode(index)
        ordered_nodes.append(node)
        arrival_times_sec.append(solution.Value(time_dim.CumulVar(index)))
        if routing.IsEnd(index):
            break
        index = solution.Value(routing.NextVar(index))

    return ordered_nodes, arrival_times_sec


# ══════════════════════════════════════════════════════════════════
#  OPTIMIZE
# ══════════════════════════════════════════════════════════════════

@app.route("/optimize", methods=["POST"])
@login_required
def optimize():
    data            = request.json or {}
    stops           = data.get("stops", [])
    start           = data.get("start") or DEFAULT_START
    start_time_hhmm = (data.get("startTime") or "09:00").strip()

    if not stops:
        return jsonify({"error": "No stops provided"}), 400

    try:
        start_minutes = hhmm_to_minutes(start_time_hhmm)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    deadline_minutes = hhmm_to_minutes(CHECKIN_DEADLINE_HHMM)

    try:
        start = {
            "name": start.get("name"),
            "lat":  float(start.get("lat")),
            "lng":  float(start.get("lng")),
        }
    except Exception:
        return jsonify({"error": "Start location must have valid lat/lng."}), 400

    cleaned_stops = []
    for s in stops:
        try:
            cleaned_stops.append({
                "name":           s.get("name"),
                "lat":            float(s.get("lat")),
                "lng":            float(s.get("lng")),
                "arrival":        bool(s.get("arrival", False)),
                "serviceMinutes": int(s.get("serviceMinutes", 60)),
            })
        except Exception:
            continue

    if not cleaned_stops:
        return jsonify({"error": "No valid stops (missing lat/lng)."}), 400

    all_locations = [start] + cleaned_stops

    coords      = ";".join(f"{float(s['lng'])},{float(s['lat'])}" for s in all_locations)
    matrix_url  = f"http://router.project-osrm.org/table/v1/driving/{coords}?annotations=duration"
    resp        = requests.get(matrix_url, timeout=30)
    if resp.status_code != 200:
        return jsonify({"error": "OSRM matrix request failed"}), 500

    duration_matrix = resp.json().get("durations")
    if not duration_matrix:
        return jsonify({"error": "Invalid matrix response"}), 500

    service_times_sec = [0] + [max(0, int(s.get("serviceMinutes", 60))) * 60 for s in cleaned_stops]
    checkin_flags     = [False] + [bool(s.get("arrival", False)) for s in cleaned_stops]

    enforce_deadline    = start_minutes < deadline_minutes
    deadline_offset_sec = (deadline_minutes - start_minutes) * 60 if enforce_deadline else None

    ordered_nodes, arrival_times_sec = None, None
    used_deadline_constraints = False
    used_soft_penalties       = False

    if enforce_deadline:
        ordered_nodes, arrival_times_sec = _solve_route(
            duration_matrix, service_times_sec, checkin_flags,
            deadline_offset_sec=deadline_offset_sec, hard_deadline=True
        )
        if ordered_nodes is not None:
            used_deadline_constraints = True

    if ordered_nodes is None:
        ordered_nodes, arrival_times_sec = _solve_route(
            duration_matrix, service_times_sec, checkin_flags,
            deadline_offset_sec=deadline_offset_sec if enforce_deadline else None,
            soft_deadline_penalty=True
        )
        if ordered_nodes is not None:
            used_soft_penalties = True

    if ordered_nodes is None:
        ordered_nodes, arrival_times_sec = _solve_route(
            duration_matrix, service_times_sec, checkin_flags
        )
        if ordered_nodes is None:
            return jsonify({"error": "No solution found"}), 500

    node_arrival_sec      = {}
    for pos, node in enumerate(ordered_nodes):
        if node not in node_arrival_sec:
            node_arrival_sec[node] = arrival_times_sec[pos]

    ordered_stop_nodes = [n for n in ordered_nodes[1:] if n != 0]
    ordered_stops      = [all_locations[n] for n in ordered_stop_nodes]

    coords_final = ";".join(f"{float(s['lng'])},{float(s['lat'])}" for s in [start] + ordered_stops)
    route_url    = f"http://router.project-osrm.org/route/v1/driving/{coords_final}?overview=full&geometries=geojson"
    route_resp   = requests.get(route_url, timeout=30)
    if route_resp.status_code != 200:
        return jsonify({"error": "OSRM route request failed"}), 500

    route_data = route_resp.json().get("routes", [{}])[0]
    if not route_data:
        return jsonify({"error": "Invalid OSRM route response"}), 500

    driving_duration = float(route_data.get("duration", 0.0))
    service_duration = sum(int(s.get("serviceMinutes", 60)) * 60 for s in ordered_stops)
    total_duration   = driving_duration + service_duration

    schedule      = []
    late_checkins = []

    for node in ordered_stop_nodes:
        stop         = all_locations[node]
        eta_minutes  = start_minutes + int(node_arrival_sec.get(node, 0) // 60)
        service_min  = int(stop.get("serviceMinutes", 60))
        finish_min   = eta_minutes + service_min
        is_checkin   = bool(stop.get("arrival", False))
        is_late      = is_checkin and finish_min > deadline_minutes

        if is_late:
            late_checkins.append(stop.get("name"))

        schedule.append({
            "name":           stop.get("name"),
            "arrival":        is_checkin,
            "late":           is_late,
            "serviceMinutes": service_min,
            "eta":            minutes_to_hhmm(eta_minutes),
            "eta_minutes":    eta_minutes,
            "lat":            float(stop.get("lat")),
            "lng":            float(stop.get("lng")),
        })

    return jsonify({
        "distance":                   route_data.get("distance", 0.0),
        "total_duration":             total_duration,
        "driving_duration":           driving_duration,
        "service_duration":           service_duration,
        "geometry":                   route_data.get("geometry"),
        "ordered_stops":              ordered_stops,
        "start_time":                 start_time_hhmm,
        "checkin_deadline":           CHECKIN_DEADLINE_HHMM,
        "schedule":                   schedule,
        "late_checkins":              late_checkins,
        "deadline_constraints_used":  used_deadline_constraints,
        "soft_penalties_used":        used_soft_penalties,
    })


# ══════════════════════════════════════════════════════════════════
#  PORTFOLIO  (public — no login required)
# ══════════════════════════════════════════════════════════════════



# ══════════════════════════════════════════════════════════════════
#  RUN
# ══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)