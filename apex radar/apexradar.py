import math
import random
import time
from collections import Counter, deque

import plotly.graph_objects as go
import streamlit as st

APP_TITLE = "ApexRadar - Smart Resort Safety & Rescue Control Center"

st.set_page_config(page_title=APP_TITLE, layout="wide")

st.markdown("""
    <style>
    .stApp { background-color: #ffffff; color: #0f172a; }
    </style>
""", unsafe_allow_html=True)

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
FLEET_SIZE = 50
FIRST_PASS = 8801
DEMO_PASS = "#8803"
DEMO_POINT = (700.0, 900.0)                   # metres east, north (inside A-3)
DANGER_RECT = (380.0, 620.0, 1020.0, 1180.0)  # x0, y0, x1, y1 in metres

ZONE_NAME = {
    "A-1": "Beginner Meadow",
    "A-3": "Tree Line Blind Spot",
    "B-2": "Intermediate Sunrise",
    "C-1": "Freestyle Park",
}
ZONE_A3 = "A-3"

G_THRESHOLD = 8.0
SIM_IMPACT_G = 9.6
IMMOBILITY_WINDOW_SEC = 20       # target design window is 180 s
CONTACT_TARGET_SEC = 5.0
CONTACT_ANSWER_P = 0.9           # placeholder: chance the supervisor answers first call
CONTACT_ANSWER_MAX_SEC = 4.5
BACKUP_CONNECT_SEC = 1.5

PATROL_SPEED_MPS = 1.9           # 400 m ≈ 3.5 min
PATROL_ETA_TARGET_SEC = 240
TABLET_PUSH_SEC = 0.5
WAYPOINT_COUNT = 4
BLE_RANGE_M = 50.0
BLE_ARM_SEC = BLE_RANGE_M / PATROL_SPEED_MPS

PREP_SEC = 120                   # on-scene triage and toboggan preparation
AMB_ETA_SEC = 480                # ALS ambulance, regional hub to base clinic (8 min)
HOSP_TRANSIT_SEC = 840           # base clinic to hospital (14 min)
CLOSE_AFTER_HOSP_SEC = 60        # patrol cleared after hospital handover
# Offsets from patrol arrival
DESCENT_OFF = PREP_SEC
HANDOVER_OFF = DESCENT_OFF + AMB_ETA_SEC
HOSPITAL_OFF = HANDOVER_OFF + HOSP_TRANSIT_SEC
CLOSE_OFF = HOSPITAL_OFF + CLOSE_AFTER_HOSP_SEC

EMERGENCY_CHANNEL = "CH-9 Emergency (PoC radio)"
CLINIC = "Base Station Medical Clinic"
REGIONAL_HUB = "regional EMS hub"
HOSPITAL = "St. Anton Alpine Trauma & Regional Medical Center"  # placeholder, confirm

TTS_TEMPLATE = (
    "High-risk accident detected at Zone {zone} ({zone_name}) "
    "coordinates ({lat}, {lon}). {team}, please proceed to inspect and rescue."
)

PHASE_NAMES = {
    0: "Immobility check",
    1: "Phase 1 · En route and proximity tracking",
    2: "Phase 2 · On-scene arrival and triage",
    3: "Phase 3 · Mountain descent and base clinic transfer",
    4: "Phase 4 · Medical handover and hospital transit",
    5: "Phase 5 · Incident closed",
}

TRAIL_WINDOW_SEC = 300
IMU_SAMPLES = 120
LOG_CAP = 300
BATTERY_PER_SEC = 0.01
REST_P_PER_SEC = 0.0005
REST_SEC = (20, 60)
SNAPSHOT_EVERY_SEC = 60

ORIGIN_LAT, ORIGIN_LON = 46.5, 11.0
M_PER_DEG_LAT = 111_320
M_PER_DEG_LON = M_PER_DEG_LAT * math.cos(math.radians(46.51))

COLORS = {
    "cruising": "#0891b2",
    "stationary": "#d97706",
    "verifying": "#ea580c",
    "sos": "#dc2626",
    "mesh": "#059669",
}
LABELS = {
    "cruising": "🟢 Cruising",
    "stationary": "🟡 Stationary",
    "verifying": "🟠 Verifying immobility",
    "sos": "🔴 SOS CRASH ALERT",
}
RANK = {"sos": 0, "verifying": 1, "stationary": 2, "cruising": 3}

# Placeholder contacts. Replace with the real roster.
ZONE_LEADS = {
    "A": ("Zone A Supervisor", "301"),
    "B": ("Zone B Supervisor", "302"),
    "C": ("Zone C Supervisor", "303"),
}
DUTY_MANAGER = ("Control Room Duty Manager", "100")

# Casualty text for the demo. Replace with patrol data.
CASUALTY = {
    "condition": "conscious, immobilized",
    "injuries": "suspected lower-leg fracture and mild concussion",
    "cause": "Lost edge on an icy patch in the tree shadow and fell at speed.",
    "action": "Preparing spinal board and mountain rescue toboggan for descent.",
}


# ----------------------------------------------------------------------------
# Geometry
# ----------------------------------------------------------------------------
def polyline_len(pts):
    return sum(math.dist(pts[i - 1], pts[i]) for i in range(1, len(pts)))


def point_along(pts, dist):
    """Position `dist` metres along a closed polyline (wraps)."""
    dist %= polyline_len(pts)
    for i in range(1, len(pts)):
        seg = math.dist(pts[i - 1], pts[i])
        if dist <= seg:
            f = dist / seg if seg else 0.0
            (ax, ay), (bx, by) = pts[i - 1], pts[i]
            return (ax + (bx - ax) * f, ay + (by - ay) * f)
        dist -= seg
    return pts[-1]


def to_gps(p):
    return (ORIGIN_LAT + p[1] / M_PER_DEG_LAT, ORIGIN_LON + p[0] / M_PER_DEG_LON)


def _loop(cx, cy, rx, ry, phase, n=32):
    pts = []
    for i in range(n):
        a = 2 * math.pi * i / n
        k = 1 + 0.08 * math.sin(3 * a + phase)
        pts.append((cx + rx * k * math.cos(a), cy + ry * k * math.sin(a)))
    return pts + [pts[0]]


TRAILS = {
    "Meadow Run": {"zone": "A-1", "pts": _loop(1500, 400, 800, 250, 0.2)},
    "Forest Link": {"zone": "A-3", "pts": _loop(700, 900, 380, 240, 0.5)},
    "Sunrise Blue": {"zone": "B-2", "pts": _loop(1550, 1100, 950, 380, 0.3)},
    "Couloir Black": {"zone": "C-1", "pts": _loop(900, 1650, 520, 330, 1.2)},
}
TRAIL_LEN = {name: polyline_len(t["pts"]) for name, t in TRAILS.items()}

TEAMS = [
    {"name": "Patrol Team Alpha", "base": "Base Station 3", "xy": (1100.0, 900.0)},
    {"name": "Patrol Team Bravo", "base": "Base Station 1", "xy": (2700.0, 1200.0)},
    {"name": "Patrol Team Charlie", "base": "Base Station 2", "xy": (300.0, 1950.0)},
]


# ----------------------------------------------------------------------------
# Domain helpers
# ----------------------------------------------------------------------------
def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def fmt_mmss(sec):
    m, s = divmod(int(round(sec)), 60)
    return f"{m}:{s:02d}"


def ts(wall):
    return time.strftime("%H:%M:%S", time.localtime(wall))


def zone_label(code):
    return f"{code} · {ZONE_NAME[code]}"


def nearest_team(p):
    team = min(TEAMS, key=lambda t: math.dist(p, t["xy"]))
    return team, math.dist(p, team["xy"])


def route_waypoints(start_xy, end_xy, n=WAYPOINT_COUNT):
    """Evenly spaced GPS waypoints from the patrol base to the casualty."""
    pts = []
    for i in range(n):
        f = i / (n - 1)
        x = start_xy[0] + (end_xy[0] - start_xy[0]) * f
        y = start_xy[1] + (end_xy[1] - start_xy[1]) * f
        pts.append(to_gps((x, y)))
    return pts


def render_tts(zone_code, zone_name, lat, lon, team):
    """Fill the broadcast template with live zone, coordinates and team."""
    return TTS_TEMPLATE.format(
        zone=zone_code, zone_name=zone_name,
        lat=f"{lat:.5f}", lon=f"{lon:.5f}", team=team,
    )


def sample_imu(rng):
    return (rng.uniform(-0.15, 0.15), rng.uniform(-0.15, 0.15), 1 + rng.uniform(-0.15, 0.15))


def impact_imu(g):
    n = math.sqrt(0.6 ** 2 + 0.4 ** 2 + 0.69 ** 2)
    return (g * 0.6 / n, g * 0.4 / n, g * 0.69 / n)


def status_of(s):
    if s["state"] in ("verifying", "sos"):
        return s["state"]
    return "stationary" if s["speed"] < 1 else "cruising"


def new_skier(i, rng):
    pass_no = FIRST_PASS + i
    pass_id = f"#{pass_no}"
    trail = "Forest Link" if pass_id == DEMO_PASS else rng.choice(list(TRAILS))
    along = rng.uniform(0, TRAIL_LEN[trail])
    cruise = rng.uniform(25, 55)
    return {
        "id": pass_id,
        "pass": pass_no,
        "trail": trail,
        "zone": TRAILS[trail]["zone"],
        "along": along,
        "cruise": cruise,
        "speed": cruise,
        "battery": rng.uniform(60, 99),
        "pos": point_along(TRAILS[trail]["pts"], along),
        "state": "cruising",          # cruising | verifying | sos
        "resting": False,
        "rest_until": 0.0,
        "rssi": rng.uniform(-75, -55),
        "loss": 0.0,
        "imu": deque(maxlen=IMU_SAMPLES),
        "history": deque(),           # (wall time, x, y)
        "gpeak": None,
        "wall0": None,                # wall time of impact
        "t": 0.0,                     # incident clock, seconds from impact
        "phase": 0,
        "dispatch": None,
        "kpi": {},
        "script": [],
    }


def clear_incident(s):
    s.update(state="cruising", speed=s["cruise"], resting=False, rest_until=0.0,
             gpeak=None, wall0=None, t=0.0, phase=0, dispatch=None, kpi={}, script=[])


# Patrol and ble helpers use the incident clock s["t"]
def patrol_fraction(d, t):
    span = d["arrive"] - d["at"]
    if span <= 0:
        return 1.0
    return clamp((t - d["at"]) / span, 0.0, 1.0)


def patrol_remaining_m(s):
    d = s["dispatch"]
    return d["distance"] * (1 - patrol_fraction(d, s["t"]))


def ble_active(s):
    d = s["dispatch"]
    return (s["state"] == "sos" and d is not None and s["t"] < d["arrive"]
            and patrol_remaining_m(s) <= BLE_RANGE_M)


def phase_progress(s):
    d, t, ph = s["dispatch"], s["t"], s["phase"]
    if ph <= 1:
        return patrol_fraction(d, t)
    if ph == 2:
        return 1.0
    if ph == 3:
        return clamp((t - (d["arrive"] + DESCENT_OFF)) / AMB_ETA_SEC, 0.0, 1.0)
    if ph == 4:
        return clamp((t - (d["arrive"] + HANDOVER_OFF)) / HOSP_TRANSIT_SEC, 0.0, 1.0)
    return 1.0


def amb_remaining(s):
    return max(0.0, s["dispatch"]["arrive"] + HANDOVER_OFF - s["t"])


def hosp_remaining(s):
    return max(0.0, s["dispatch"]["arrive"] + HOSPITAL_OFF - s["t"])


def patrol_marker(s):
    d = s["dispatch"]
    bx, by = d["team"]["xy"]
    cx, cy = s["pos"]
    ph = s["phase"]
    if ph <= 1:
        f = patrol_fraction(d, s["t"])
        return (bx + (cx - bx) * f, by + (cy - by) * f), ("en route" if f < 1 else "on scene")
    if ph == 2:
        return (cx, cy), "on scene"
    if ph == 3:
        f = phase_progress(s)
        return (cx + (bx - cx) * f, cy + (by - cy) * f), "toboggan"
    return (bx, by), "at clinic"


# ----------------------------------------------------------------------------
# Logs
# ----------------------------------------------------------------------------
def log_general(msg, wall=None):
    st.session_state.glog.append({"t": time.time() if wall is None else wall, "msg": msg})
    del st.session_state.glog[:-LOG_CAP]


def log_incident(source, kind, msg, skier, t=None):
    """Incident events carry the incident clock (t) and the matching wall time."""
    t = skier["t"] if t is None else t
    st.session_state.ilog.append({
        "wall": skier["wall0"] + t,
        "t": t,
        "pass": skier["id"],
        "source": source,
        "kind": kind,
        "msg": msg,
    })
    del st.session_state.ilog[:-LOG_CAP]


# ----------------------------------------------------------------------------
# Incident script. Events are absolute (ev_abs) or anchored to patrol arrival
# (ev_rel). Anchored events move when the patrol is marked on scene early.
# ----------------------------------------------------------------------------
def ev_abs(t, source, kind, msg, milestone=None, phase=None):
    return {"t": t, "anchor": False, "off": 0.0, "source": source, "kind": kind,
            "msg": msg, "milestone": milestone, "phase": phase}


def ev_rel(arrive, off, source, kind, msg, milestone=None, phase=None):
    return {"t": arrive + off, "anchor": True, "off": off, "source": source, "kind": kind,
            "msg": msg, "milestone": milestone, "phase": phase}


def deliver(s):
    """Write every scheduled event that is due to the incident log."""
    due = [e for e in s["script"] if e["t"] <= s["t"]]
    if not due:
        return
    s["script"] = [e for e in s["script"] if e["t"] > s["t"]]
    for e in sorted(due, key=lambda e: e["t"]):
        if e["phase"] is not None:
            s["phase"] = e["phase"]
        if e["milestone"]:
            s["kpi"][e["milestone"]] = e["t"]
        log_incident(e["source"], e["kind"], e["msg"], s, e["t"])


def confirm(s, rng):
    """Immobility confirmed: dispatch, contact zone, schedule the full response."""
    now_t = s["t"]
    team, dist = nearest_team(s["pos"])
    eta = dist / PATROL_SPEED_MPS
    arrive = now_t + eta
    zone = s["zone"]
    zone_name = ZONE_NAME[zone]
    lat, lon = to_gps(s["pos"])
    where = f"{lat:.5f}, {lon:.5f}"
    tts = render_tts(zone, zone_name, lat, lon, team["name"])
    lead_name, lead_ext = ZONE_LEADS[zone[0]]
    base = team["base"]

    s.update(state="sos", resting=False, speed=0.0)
    s["dispatch"] = {
        "team": team,
        "distance": dist,
        "eta": eta,
        "at": now_t,
        "arrive": arrive,
        "tts": tts,
        "waypoints": route_waypoints(team["xy"], s["pos"]),
        "lat": lat,
        "lon": lon,
    }

    ev = [
        ev_abs(now_t, "Edge IMU", "alert",
               f"SOS confirmed: no movement for {IMMOBILITY_WINDOW_SEC} s after "
               f"{s['gpeak']:.1f} g impact at {where}, Zone {zone_label(zone)}"),
        ev_abs(now_t, "Control room", "dispatch",
               f"Dispatch broadcast to {team['name']} ({base}): {dist:.0f} m, "
               f"ETA {fmt_mmss(eta)}, target {where}", phase=1),
        ev_abs(now_t, "Radio", "comms",
               f"Switched to {EMERGENCY_CHANNEL} for {team['name']}"),
        ev_abs(now_t, "TTS broadcast", "comms",
               f'Voice broadcast on {EMERGENCY_CHANNEL}: "{tts}"'),
        ev_abs(now_t + TABLET_PUSH_SEC, "Rugged tablet", "comms",
               f"{WAYPOINT_COUNT} waypoints pushed to {team['name']}; final waypoint {where}; "
               "navigation active"),
        ev_abs(now_t + rng.uniform(2.0, 4.0), team["name"], "dispatch",
               f"Acknowledged dispatch, departing {base}"),
    ]

    if rng.random() < CONTACT_ANSWER_P:
        contact_sec = rng.uniform(1.2, CONTACT_ANSWER_MAX_SEC)
        ev.append(ev_abs(now_t + contact_sec, lead_name, "contact",
                         f"Connected in {contact_sec:.1f} s (ext. {lead_ext}); briefed on {s['id']}"))
    else:
        contact_sec = CONTACT_TARGET_SEC + BACKUP_CONNECT_SEC
        ev.append(ev_abs(now_t + CONTACT_TARGET_SEC, "Control room", "contact",
                         f"No answer from {lead_name} (ext. {lead_ext}) after "
                         f"{CONTACT_TARGET_SEC:.0f} s; escalating to {DUTY_MANAGER[0]} "
                         f"(ext. {DUTY_MANAGER[1]})"))
        ev.append(ev_abs(now_t + contact_sec, DUTY_MANAGER[0], "contact",
                         f"Connected in {contact_sec:.1f} s; contact target missed, zone briefed"))

    s["kpi"] = {
        "contact_sec": contact_sec,
        "contact_met": contact_sec <= CONTACT_TARGET_SEC,
        "connect_at": now_t + contact_sec,
        "skipped": False,
        "on_scene": None,
        "descent": None,
        "handover": None,
        "hospital": None,
        "closed": None,
    }

    ev += [
        ev_rel(arrive, -BLE_ARM_SEC, "BLE finder", "search",
               f"Within {BLE_RANGE_M:.0f} m of casualty: BLE RSSI proximity search armed for {s['id']}"),
        ev_rel(arrive, 0, team["name"], "arrival",
               f"On scene at {where}. Radio check OK. Casualty located",
               milestone="on_scene", phase=2),
        ev_rel(arrive, 15, team["name"], "field",
               f"Triage: patient {s['id']} {CASUALTY['condition']}; {CASUALTY['injuries']}. "
               f"High-G impact verified at {s['gpeak']:.1f} g"),
        ev_rel(arrive, 30, team["name"], "field", f"Cause: {CASUALTY['cause']}"),
        ev_rel(arrive, 60, team["name"], "field", f"Action: {CASUALTY['action']}"),
        ev_rel(arrive, DESCENT_OFF, team["name"], "field",
               f"Toboggan descent started from Zone {zone} to {CLINIC}",
               milestone="descent", phase=3),
        ev_rel(arrive, DESCENT_OFF, "Control room", "dispatch",
               f"ALS ground ambulance dispatched from {REGIONAL_HUB}; "
               f"ETA at {CLINIC} {fmt_mmss(AMB_ETA_SEC)}"),
        ev_rel(arrive, HANDOVER_OFF, "Base clinic", "handover",
               f"Patient transferred to EMS ambulance at {CLINIC}",
               milestone="handover", phase=4),
        ev_rel(arrive, HANDOVER_OFF, "Ambulance", "field",
               f"Receiving facility: {HOSPITAL}. Hospital transit ETA {fmt_mmss(HOSP_TRANSIT_SEC)}"),
        ev_rel(arrive, HOSPITAL_OFF, "Ambulance", "field",
               f"Patient handed to {HOSPITAL}", milestone="hospital"),
        ev_rel(arrive, CLOSE_OFF, "Control room", "closed",
               f"Incident closed. {team['name']} cleared and returned to active mountain "
               "patrol rotation. Event log archived.",
               milestone="closed", phase=5),
    ]
    s["script"] = sorted(ev, key=lambda e: e["t"])


def skip_eta_action():
    s = by_id(st.session_state.active)
    d = s["dispatch"]
    if s["phase"] != 1 or s["t"] >= d["arrive"]:
        return
    now_t = s["t"]
    d["arrive"] = now_t
    s["kpi"]["skipped"] = True
    for e in s["script"]:
        if e["anchor"]:
            e["t"] = max(now_t, now_t + e["off"])
    log_incident("Control room", "skip",
                 f"{d['team']['name']} on scene early; ETA countdown skipped", s)


def advance_phase_action():
    s = by_id(st.session_state.active)
    targets = [e["t"] for e in s["script"] if e["phase"] is not None and e["t"] > s["t"]]
    if not targets:
        return
    s["t"] = min(targets)
    log_incident("Control room", "skip",
                 "Demo: advanced to the next phase boundary", s)


def next_phase_exists(s):
    return any(e["phase"] is not None and e["t"] > s["t"] for e in s["script"])


def trigger_demo():
    s = by_id(DEMO_PASS)
    if s["state"] != "cruising":
        return
    wall = time.time()
    clear_incident(s)
    s.update(
        state="verifying",
        resting=False,
        pos=DEMO_POINT,
        gpeak=SIM_IMPACT_G,
        wall0=wall,
        zone=ZONE_A3,
    )
    s["imu"].append(impact_imu(SIM_IMPACT_G))
    st.session_state.active = DEMO_PASS
    st.session_state.focus = DEMO_PASS
    log_general(f"Demo incident started on {DEMO_PASS}", wall)
    log_incident("Edge IMU", "alert",
                 f"High-G impact {SIM_IMPACT_G} g on {DEMO_PASS}, Zone {zone_label(ZONE_A3)}. "
                 f"Immobility check started ({IMMOBILITY_WINDOW_SEC} s)", s)


def close_action():
    s = by_id(st.session_state.active)
    count = sum(1 for e in st.session_state.ilog if e["pass"] == s["id"])
    log_incident("Control room", "system",
                 f"Event log archived for {s['id']} ({count} entries). Pass released to the fleet; "
                 "post-incident review within 24 h: A-3 blind-spot cause, signage and grooming check",
                 s)
    log_general(f"Incident closed for {s['id']}; pass returned to the fleet")
    clear_incident(s)
    st.session_state.active = None


def reset_all():
    now = time.time()
    st.session_state.rng = random.Random()
    st.session_state.fleet = [new_skier(i, st.session_state.rng) for i in range(FLEET_SIZE)]
    st.session_state.glog = []
    st.session_state.ilog = []
    st.session_state.active = None
    st.session_state.focus = f"#{FIRST_PASS}"
    st.session_state.last_t = now
    st.session_state.last_snapshot = now
    log_general(f"Fleet online: {FLEET_SIZE} smart passes", now)


def step(fleet, now, dt, rng):
    """Advance the simulation by dt seconds and deliver scheduled events."""
    for s in fleet:
        if s["state"] == "cruising":
            if s["resting"] and now >= s["rest_until"]:
                s["resting"] = False
                log_general(f"{s['id']} resumed skiing on {s['trail']}", now)
            if not s["resting"] and rng.random() < REST_P_PER_SEC * dt:
                s["resting"] = True
                s["rest_until"] = now + rng.uniform(*REST_SEC)
                log_general(f"{s['id']} stopped for a rest on {s['trail']}", now)
            if s["resting"]:
                s["speed"] = 0.0
            else:
                s["speed"] = s["cruise"] * rng.uniform(0.97, 1.03)
                s["along"] += s["speed"] / 3.6 * dt
                s["pos"] = point_along(TRAILS[s["trail"]]["pts"], s["along"])
        elif s["state"] == "verifying":
            s["t"] += dt
            if s["t"] >= IMMOBILITY_WINDOW_SEC:
                confirm(s, rng)
        elif s["state"] == "sos":
            s["t"] += dt

        if ble_active(s):
            rem = patrol_remaining_m(s)
            s["rssi"] = clamp(-40 - 20 * math.log10(max(rem, 1.0)) + rng.uniform(-1, 1), -90, -35)
        else:
            s["rssi"] = clamp(s["rssi"] + rng.uniform(-2, 2), -90, -35)

        s["battery"] = max(0.0, s["battery"] - BATTERY_PER_SEC * dt)
        s["loss"] = 0.0 if rng.random() < 0.85 else round(rng.uniform(0.1, 5.0), 1)
        s["imu"].append(sample_imu(rng))
        s["history"].append((now, s["pos"][0], s["pos"][1]))
        while s["history"] and s["history"][0][0] < now - TRAIL_WINDOW_SEC:
            s["history"].popleft()

    for s in fleet:
        deliver(s)


def snapshot_if_due(fleet, now):
    if now - st.session_state.last_snapshot < SNAPSHOT_EVERY_SEC:
        return
    st.session_state.last_snapshot = now
    counts = Counter(status_of(s) for s in fleet)
    avg_loss = sum(s["loss"] for s in fleet) / len(fleet)
    log_general(
        f"Fleet snapshot: {counts['cruising']} cruising · {counts['stationary']} stationary · "
        f"{counts['verifying']} verifying · {counts['sos']} SOS · mesh loss avg {avg_loss:.1f}%",
        now,
    )


def by_id(pass_id):
    return next(s for s in st.session_state.fleet if s["id"] == pass_id)


if "fleet" not in st.session_state or "ilog" not in st.session_state:
    reset_all()


# ----------------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------------
def fleet_rows(fleet):
    rows = []
    for s in sorted(fleet, key=lambda x: (RANK[status_of(x)], x["pass"])):
        lat, lon = to_gps(s["pos"])
        rows.append({
            "Pass ID": s["id"],
            "Zone": zone_label(s["zone"]),
            "Speed (km/h)": round(s["speed"], 1),
            "Battery": f"{s['battery']:.0f}%",
            "Status": LABELS[status_of(s)],
            "GPS": f"{lat:.5f}, {lon:.5f}",
        })
    return rows


def build_map(fleet, now):
    fig = go.Figure()

    for name, t in TRAILS.items():
        xs, ys = zip(*t["pts"])
        fig.add_trace(go.Scatter(
            x=xs, y=ys, mode="lines",
            line=dict(color="#94a3b8", width=14), opacity=0.25,
            hovertext=f"{name} · {t['zone']}", hoverinfo="text", showlegend=False,
        ))

    x0, y0, x1, y1 = DANGER_RECT
    fig.add_shape(
        type="rect", x0=x0, y0=y0, x1=x1, y1=y1, layer="below",
        line=dict(color="#d97706", dash="dash", width=1.5),
        fillcolor="rgba(217,119,6,0.07)",
    )
    fig.add_annotation(
        x=x0 + 10, y=y1 - 20, text=f"A-3 · {ZONE_NAME['A-3']}", showarrow=False,
        xanchor="left", yanchor="top", font=dict(color="#b45309", size=11),
    )

    for s in fleet:
        if len(s["history"]) > 1:
            fig.add_trace(go.Scatter(
                x=[p[1] for p in s["history"]], y=[p[2] for p in s["history"]],
                mode="lines", line=dict(color=COLORS[status_of(s)], width=1.5),
                opacity=0.45, hoverinfo="skip", showlegend=False,
            ))

    for key in ("cruising", "stationary", "verifying", "sos"):
        group = [s for s in fleet if status_of(s) == key]
        if not group:
            continue
        fig.add_trace(go.Scatter(
            x=[s["pos"][0] for s in group],
            y=[s["pos"][1] for s in group],
            mode="markers+text" if key == "sos" else "markers",
            text=[s["id"] for s in group] if key == "sos" else None,
            textposition="top center",
            marker=dict(size=14 if key == "sos" else 10, color=COLORS[key],
                        line=dict(color="white", width=1.5)),
            hovertext=[f"{s['id']} · {LABELS[key]}<br>{s['speed']:.0f} km/h · {s['battery']:.0f}%"
                       for s in group],
            hoverinfo="text",
            name=LABELS[key],
        ))

    pulse = 26 if int(now) % 2 == 0 else 44
    for s in fleet:
        if status_of(s) == "sos":
            fig.add_trace(go.Scatter(
                x=[s["pos"][0]], y=[s["pos"][1]], mode="markers",
                marker=dict(size=pulse, color="rgba(220,38,38,0.18)"),
                hoverinfo="skip", showlegend=False,
            ))

    fig.add_trace(go.Scatter(
        x=[t["xy"][0] for t in TEAMS],
        y=[t["xy"][1] for t in TEAMS],
        mode="markers+text",
        marker=dict(symbol="diamond", size=13, color=COLORS["mesh"]),
        text=[t["name"].replace("Patrol Team ", "") for t in TEAMS],
        textposition="bottom center",
        hovertext=[f"{t['name']} · {t['base']}" for t in TEAMS],
        hoverinfo="text",
        name="Patrol base",
    ))

    for s in fleet:
        if s["state"] != "sos" or not s["dispatch"]:
            continue
        d = s["dispatch"]
        bx, by = d["team"]["xy"]
        cx, cy = s["pos"]
        if s["phase"] <= 3:
            fig.add_trace(go.Scatter(
                x=[bx, cx], y=[by, cy], mode="lines",
                line=dict(color=COLORS["mesh"], width=2, dash="dot"),
                hoverinfo="skip", showlegend=False,
            ))
        (px, py), label = patrol_marker(s)
        fig.add_trace(go.Scatter(
            x=[px], y=[py], mode="markers+text",
            marker=dict(symbol="diamond", size=14, color=COLORS["mesh"],
                        line=dict(color="white", width=1.5)),
            text=[f"{d['team']['name'].replace('Patrol Team ', '')} {label}"],
            textposition="top center",
            hovertext=f"{d['team']['name']} · {label}",
            hoverinfo="text",
            showlegend=False,
        ))

    fig.update_layout(
        height=520,
        margin=dict(l=8, r=8, t=8, b=8),
        plot_bgcolor="#f8fafc",
        paper_bgcolor="white",
        xaxis=dict(range=[0, 3000], visible=False, fixedrange=True),
        yaxis=dict(range=[0, 2200], visible=False, scaleanchor="x", fixedrange=True),
        legend=dict(orientation="h", y=-0.02, x=0),
    )
    return fig


def ble_state(s):
    d = s["dispatch"]
    rem = patrol_remaining_m(s)
    if s["t"] >= d["arrive"]:
        return "On scene", "Proximity search stopped; patrol at casualty"
    if ble_active(s):
        return "Active", f"RSSI {s['rssi']:.0f} dBm · about {rem:.0f} m to casualty"
    return "Standby", f"Arms at {BLE_RANGE_M:.0f} m · {rem:.0f} m to casualty"


def ambulance_state(s):
    ph = s["phase"]
    if ph < 3:
        return "Not dispatched", f"Dispatches when the descent starts ({REGIONAL_HUB})"
    if ph == 3:
        return "Dispatched", f"From {REGIONAL_HUB}; ETA at {CLINIC} {fmt_mmss(amb_remaining(s))}"
    if ph == 4:
        return "Transporting", f"Patient aboard; hospital ETA {fmt_mmss(hosp_remaining(s))}"
    return "Released", "Ambulance released after hospital handover"


def comms_table(s):
    d = s["dispatch"]
    team = d["team"]
    if s["t"] < d["at"] + TABLET_PUSH_SEC:
        tablet = ("Pending", "Pushing waypoints")
    elif s["phase"] <= 1:
        tablet = ("Navigation active",
                  f"{WAYPOINT_COUNT} waypoints · final {d['lat']:.5f}, {d['lon']:.5f}")
    else:
        tablet = ("Route complete", "Casualty tracked to handover")
    ble = ble_state(s)
    amb = ambulance_state(s)
    st.dataframe([
        {"Channel": "PoC radio", "Status": "Connected", "Detail": f"{EMERGENCY_CHANNEL} · {team['name']}"},
        {"Channel": "Voice broadcast (TTS)", "Status": "Played",
         "Detail": "Template filled from live zone, coordinates and team"},
        {"Channel": "Rugged field tablet", "Status": tablet[0], "Detail": tablet[1]},
        {"Channel": "BLE proximity finder", "Status": ble[0], "Detail": ble[1]},
        {"Channel": "ALS ambulance", "Status": amb[0], "Detail": amb[1]},
    ], use_container_width=True, hide_index=True)


def process_table(s):
    k = s["kpi"]
    d = s["dispatch"]
    t = s["t"]
    early = " · on scene early, countdown skipped" if k.get("skipped") else ""
    contact = (f"{k['contact_sec']:.1f} s, met" if k["contact_met"]
               else f"{k['contact_sec']:.1f} s, missed (escalated)")
    eta_result = (f"{fmt_mmss(d['eta'])}, "
                  + ("met" if d["eta"] <= PATROL_ETA_TARGET_SEC else "over") + early)
    ble = "Armed" if t >= d["arrive"] - BLE_ARM_SEC else "Pending"
    on_scene = f"Confirmed at T+{fmt_mmss(k['on_scene'])}" if k["on_scene"] is not None else "Pending"
    handover = f"T+{fmt_mmss(k['handover'])}" if k["handover"] is not None else "Pending"
    hospital = f"T+{fmt_mmss(k['hospital'])}" if k["hospital"] is not None else "Pending"
    closed = f"T+{fmt_mmss(k['closed'])}" if k["closed"] is not None else "Pending"
    st.dataframe([
        {"Step": "Immobility confirmed", "Target": f"{IMMOBILITY_WINDOW_SEC} s", "Result": "Met"},
        {"Step": "Zone supervisor contact", "Target": f"≤ {CONTACT_TARGET_SEC:.0f} s", "Result": contact},
        {"Step": "Patrol dispatched", "Target": "At confirmation", "Result": "Broadcast"},
        {"Step": "Patrol ETA", "Target": f"≤ {fmt_mmss(PATROL_ETA_TARGET_SEC)}", "Result": eta_result},
        {"Step": "BLE proximity search", "Target": f"Armed at {BLE_RANGE_M:.0f} m", "Result": ble},
        {"Step": "On-scene check-in", "Target": "On arrival", "Result": on_scene},
        {"Step": "Ambulance at base clinic", "Target": f"≤ {fmt_mmss(AMB_ETA_SEC)} from descent",
         "Result": handover},
        {"Step": "Hospital transit", "Target": f"≤ {fmt_mmss(HOSP_TRANSIT_SEC)}", "Result": hospital},
        {"Step": "Incident closed", "Target": "After hospital handover", "Result": closed},
    ], use_container_width=True, hide_index=True)


def phase_status(s):
    d = s["dispatch"]
    team = d["team"]
    ph = s["phase"]
    if ph == 1:
        st.markdown(
            f"**Status:** {team['name']} en route. Distance: {patrol_remaining_m(s):.0f} m · "
            f"ETA: {fmt_mmss(max(0.0, d['arrive'] - s['t']))}\n\n"
            "**Equipment:** rugged tablet navigation active; BLE RSSI finder "
            + ("active." if ble_active(s) else "on standby (arms at 50 m).")
        )
    elif ph == 2:
        st.markdown(
            f"**Status:** {team['name']} arrived on scene.\n\n"
            f"**Field report generated:** patient {s['id']} {CASUALTY['condition']} with "
            f"{CASUALTY['injuries']} (high-G impact verified at {s['gpeak']:.1f} g).\n\n"
            f"**Action:** {CASUALTY['action']}"
        )
    elif ph == 3:
        st.markdown(
            f"**Status:** toboggan transport in progress from Zone {zone_label(s['zone'])} "
            f"to {CLINIC}.\n\n"
            f"**Ambulance:** ALS ground ambulance dispatched from {REGIONAL_HUB}, estimated arrival "
            f"at {CLINIC} in {fmt_mmss(amb_remaining(s))}."
        )
    elif ph == 4:
        st.markdown(
            f"**Status:** patient successfully transferred to EMS ambulance at {CLINIC}.\n\n"
            f"**Receiving facility:** {HOSPITAL}\n\n"
            f"**Hospital transit ETA:** {fmt_mmss(hosp_remaining(s))}"
        )
    else:
        st.markdown(
            f"**Status:** incident closed. {team['name']} cleared and returned to active "
            "mountain patrol rotation. Event log archived."
        )


def phase_text(s):
    d = s["dispatch"]
    team = d["team"]["name"]
    ph = s["phase"]
    if ph == 1:
        return f"{team} en route · {fmt_mmss(max(0.0, d['arrive'] - s['t']))} remaining"
    if ph == 2:
        return f"{team} on scene"
    if ph == 3:
        return f"Toboggan descent · {fmt_mmss(amb_remaining(s))} to clinic"
    if ph == 4:
        return f"Hospital transit · {fmt_mmss(hosp_remaining(s))} remaining"
    return "Incident closed"


def dispatch_panel():
    s = by_id(st.session_state.active) if st.session_state.active else None
    if s is None:
        st.success("System status: normal. 50 of 50 smart passes online. No active incidents.")
        return

    if s["state"] == "verifying":
        st.warning(
            f"High-G impact {s['gpeak']:.1f} g · {s['id']} · Zone {zone_label(s['zone'])}. "
            "Verifying immobility before SOS confirmation."
        )
        elapsed = min(s["t"], IMMOBILITY_WINDOW_SEC)
        st.progress(
            min(1.0, s["t"] / IMMOBILITY_WINDOW_SEC),
            text=f"Immobility check {elapsed:.0f} / {IMMOBILITY_WINDOW_SEC} s",
        )
        return

    d = s["dispatch"]
    k = s["kpi"]
    team = d["team"]
    ph = s["phase"]
    remaining = max(0.0, d["arrive"] - s["t"])
    contact_done = s["t"] >= k["connect_at"]
    contact_line = (
        f"* Zone supervisor contact: {k['contact_sec']:.1f} s, "
        f"{'within' if k['contact_met'] else 'over'} the {CONTACT_TARGET_SEC:.0f} s target"
        if contact_done
        else f"* Zone supervisor contact: calling, target {CONTACT_TARGET_SEC:.0f} s"
    )
    lat, lon = d["lat"], d["lon"]

    st.error(f"SOS CRASH ALERT · {s['id']} · Zone {zone_label(s['zone'])}")
    st.markdown("#### Automated patrol dispatch notice")
    st.success(
        "Dispatch signal broadcasted successfully\n\n"
        f"* Assigned unit: {team['name']} ({team['base']})\n"
        f"* Distance to target: {d['distance']:.0f} m\n"
        f"* Estimated response time (ETA): {fmt_mmss(d['eta'])}\n"
        f"{contact_line}\n"
        f"* Coordinates: {lat:.5f}, {lon:.5f}\n"
        f"* Channel: {EMERGENCY_CHANNEL}"
    )
    st.info(f'Voice broadcast on {EMERGENCY_CHANNEL}: "{d["tts"]}"')

    st.markdown(f"#### Live status · {PHASE_NAMES[ph]}")
    st.progress(phase_progress(s), text=phase_text(s))
    phase_status(s)

    c1, c2, c3 = st.columns(3)
    c1.metric("Patrol ETA", "On scene" if ph >= 2 else fmt_mmss(remaining))
    c2.metric("Zone contact", f"{k['contact_sec']:.1f} s" if contact_done else "Calling")
    c3.metric("Current phase", f"{ph} / 5")

    b1, b2 = st.columns(2)
    b1.button(
        "Patrol on scene: skip ETA countdown",
        on_click=skip_eta_action,
        disabled=ph != 1,
        use_container_width=True,
    )
    b2.button(
        "Advance to next phase (demo)",
        on_click=advance_phase_action,
        disabled=not next_phase_exists(s),
        use_container_width=True,
    )

    st.markdown("#### Dual-channel and frontline gear status")
    comms_table(s)

    st.markdown("#### Response process vs targets")
    process_table(s)

    st.button(
        "Close incident and release pass",
        on_click=close_action,
        disabled=k["closed"] is None,
        use_container_width=True,
    )


def imu_panel(s):
    st.markdown(f"### IMU Acceleration (3-axis, g) · {s['id']}")
    samples = list(s["imu"])
    fig = go.Figure()
    for label, color, idx in (("gX", "#0891b2", 0), ("gY", "#059669", 1), ("gZ", "#d97706", 2)):
        fig.add_trace(go.Scatter(
            x=list(range(len(samples))),
            y=[v[idx] for v in samples],
            mode="lines", name=label, line=dict(color=color, width=2),
        ))
    fig.add_hline(
        y=G_THRESHOLD, line_dash="dash", line_color="#dc2626",
        annotation_text=f"{G_THRESHOLD:g} g trigger",
    )
    fig.update_layout(
        height=230,
        margin=dict(l=10, r=10, t=10, b=10),
        plot_bgcolor="#f8fafc",
        paper_bgcolor="white",
        yaxis=dict(range=[-2, 12], title="g"),
        xaxis=dict(title="samples (1 Hz)", showticklabels=False),
        legend=dict(orientation="h", y=1.12),
    )
    st.plotly_chart(fig, use_container_width=True)
    peak = max((math.hypot(*v) for v in samples), default=0.0)
    st.caption(
        f"Peak |a| {peak:.2f} g. The 9-axis IMU's gyro and magnetometer streams "
        "are not plotted in this demo."
    )


def link_panel(fleet, focus):
    st.markdown("### IoT Mesh & Link Status")
    avg_loss = sum(s["loss"] for s in fleet) / len(fleet)
    c1, c2, c3 = st.columns(3)
    c1.metric("Active passes", f"{len(fleet)} / {FLEET_SIZE}")
    c2.metric("Mesh packet loss (avg)", f"{avg_loss:.1f}%")
    c3.metric(f"BLE RSSI · {focus['id']}", f"{focus['rssi']:.0f} dBm")
    st.markdown(
        f'<span style="color:{COLORS["mesh"]}">Mesh active</span> · emergency channel reserved',
        unsafe_allow_html=True,
    )


def log_panel():
    st.markdown("### Event log")
    tab_general, tab_incident = st.tabs(["General (dashboard status)", "Incident (collision events)"])

    with tab_general:
        if not st.session_state.glog:
            st.caption("No events yet.")
        else:
            rows = [{"Time": ts(e["t"]), "Event": e["msg"]}
                    for e in sorted(st.session_state.glog, key=lambda e: e["t"], reverse=True)]
            st.dataframe(rows, use_container_width=True, hide_index=True, height=360)

    with tab_incident:
        order = st.radio(
            "Order", ["Timeline (oldest first)", "Newest first"],
            horizontal=True, key="incident_order",
        )
        items = sorted(st.session_state.ilog, key=lambda e: e["t"],
                       reverse=order.startswith("Newest"))
        if not items:
            st.caption("No incident events yet.")
        else:
            rows = [{
                "Time": ts(e["wall"]),
                "T+": f"T+{fmt_mmss(e['t'])}",
                "Pass": e["pass"],
                "Source": e["source"],
                "Type": e["kind"],
                "Event": e["msg"],
            } for e in items]
            st.dataframe(rows, use_container_width=True, hide_index=True, height=420)


# ----------------------------------------------------------------------------
# Live dashboard (re-runs every second)
# ----------------------------------------------------------------------------
@st.fragment(run_every=1)
def dashboard():
    now = time.time()
    dt = min(max(0.0, now - st.session_state.last_t), 2.0)
    st.session_state.last_t = now
    fleet = st.session_state.fleet
    step(fleet, now, dt, st.session_state.rng)
    snapshot_if_due(fleet, now)

    demo = by_id(DEMO_PASS)
    sos_count = sum(status_of(s) == "sos" for s in fleet)

    st.title(APP_TITLE)
    st.caption(
        "Smart lift pass edition · Real-time fleet telemetry, edge state machine, "
        "emergency dispatch and patrol coordination"
    )
    st.markdown("---")

    left, right = st.columns([1.3, 1])

    with left:
        st.subheader("Live Skier Fleet Overview & Trajectories")
        st.info(
            f"Real-time telemetry for {len(fleet)} smart passes (#8801 – #8850) via IoT mesh. "
            f"Active SOS: {sos_count}"
        )
        st.dataframe(fleet_rows(fleet), use_container_width=True, hide_index=True)

        st.markdown("### Interactive Resort Map & Trajectory Radar")
        st.plotly_chart(build_map(fleet, now), use_container_width=True)

        b1, b2 = st.columns(2)
        b1.button(
            f"Demo Zone A-3 Crash ({DEMO_PASS})",
            type="primary",
            use_container_width=True,
            on_click=trigger_demo,
            disabled=demo["state"] != "cruising",
        )
        b2.button("Reset Fleet", use_container_width=True, on_click=reset_all)

    with right:
        st.subheader("Real-time IMU Telemetry & Rescue Dispatch")
        dispatch_panel()
        st.markdown("---")
        st.selectbox("Telemetry pass", [s["id"] for s in fleet], key="focus")
        focus = by_id(st.session_state.focus)
        imu_panel(focus)
        link_panel(fleet, focus)

    st.markdown("---")
    log_panel()


dashboard()
