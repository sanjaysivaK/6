# app.py
"""
River Flood Dashboard backend (Flask + SQLite)
- No Backendless: uses local SQLite for everything
- Serial (Arduino) reading if pyserial available
- Weather via OpenWeatherMap
- Twilio SMS for alerts
- Background threads:
    * serial_reader_thread: reads serial and updates in-memory latest & optionally saves every minute
    * periodic_saver_thread: ensures a value saved every minute if available
    * alert_monitor_thread: checks saved alert config every 10s, computes risk and sends SMS once per crossing
Notes (Option A behavior):
- Sensor data is used ONLY when the caller sends use_sensor=True (live location).
- Other locations always use weather-only risk.
- Alert config supports a use_sensor flag so alerts can be configured to use sensor data when appropriate.
"""
from flask import Flask, render_template, request, jsonify
import sqlite3
import threading
import time
import requests
import logging
from datetime import datetime
import math
import random

# Optional: serial
try:
    import serial
    from serial.serialutil import SerialException
except Exception:
    serial = None
    SerialException = Exception

# Optional: Twilio
try:
    from twilio.rest import Client as TwilioClient
    from twilio.base.exceptions import TwilioRestException
except Exception:
    TwilioClient = None
    TwilioRestException = Exception

# ---------- CONFIG ----------
DATABASE = "water_alerts.db"

OPENWEATHER_API_KEY = "3254c4b73123090d49690ebbcc783274"

TWILIO_SID = "AC1624e2929dca14106e69f394eefcdfe3"
TWILIO_TOKEN = "3669f663bca1b9847a2c093ccb41faf1"
TWILIO_FROM = "+19787836524"

SERIAL_PORT = "COM9"
SERIAL_BAUD = 9600

# background timings
SAVED_EVERY_SECONDS = 60    # save sensor to DB every 60s
ALERT_CHECK_SECONDS = 10    # check alert every 10s

# ---------- FLASK ----------
app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# ---------- GLOBAL STATE ----------
_last_live = None       # last numeric distance reading (cm)
_last_live_ts = None
_last_live_lock = threading.Lock()
_stop_event = threading.Event()
_serial_obj = None

# ---------- DATABASE HELPERS ----------
def get_conn():
    conn = sqlite3.connect(DATABASE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    cur = conn.cursor()

    # water_levels table
    cur.execute("""
    CREATE TABLE IF NOT EXISTS water_levels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        distance REAL NOT NULL,
        timestamp TEXT NOT NULL
    );
    """)

    # alert_config table (includes use_sensor flag)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS alert_config (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        threshold REAL,
        phone TEXT,
        enabled INTEGER DEFAULT 0,
        lat REAL,
        lon REAL,
        last_sent TEXT,
        active INTEGER DEFAULT 0,
        use_sensor INTEGER DEFAULT 0,
        updated_at TEXT
    );
    """)

    # weather_cache table
    cur.execute("""
    CREATE TABLE IF NOT EXISTS weather_cache (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        lat REAL,
        lon REAL,
        payload TEXT,
        timestamp TEXT
    );
    """)

    conn.commit()

    # In case older DB exists without use_sensor column, attempt to add it (safe to ignore errors)
    try:
        cur.execute("ALTER TABLE alert_config ADD COLUMN use_sensor INTEGER DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError:
        # column already exists or cannot be added; ignore
        pass
    finally:
        conn.close()

def insert_water(distance, ts=None):
    ts = ts or datetime.utcnow().isoformat()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("INSERT INTO water_levels(distance,timestamp) VALUES (?, ?)", (float(distance), ts))
    conn.commit()
    conn.close()

def fetch_recent(limit=30):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT distance,timestamp FROM water_levels ORDER BY timestamp DESC LIMIT ?", (limit,))
    rows = cur.fetchall()
    conn.close()
    return [{"distance": r["distance"], "timestamp": r["timestamp"]} for r in rows]

def fetch_latest_row():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT distance,timestamp FROM water_levels ORDER BY timestamp DESC LIMIT 1")
    r = cur.fetchone()
    conn.close()
    if r:
        return {"distance": r["distance"], "timestamp": r["timestamp"]}
    return None

# Alert config (single global)
def get_alert_config():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM alert_config ORDER BY id DESC LIMIT 1")
    r = cur.fetchone()
    conn.close()
    return dict(r) if r else None

def save_alert_config(threshold, phone, enabled, lat, lon, use_sensor):
    now = datetime.utcnow().isoformat()
    conn = get_conn()
    cur = conn.cursor()
    # keep only one config -> delete previous
    cur.execute("DELETE FROM alert_config")
    # Insert new config including use_sensor flag (0/1)
    cur.execute("""INSERT INTO alert_config(threshold,phone,enabled,lat,lon,last_sent,active,use_sensor,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""", (threshold, phone, 1 if enabled else 0, lat, lon, None, 0, 1 if use_sensor else 0, now))
    conn.commit()
    conn.close()
    logging.info("Saved alert config threshold=%s phone=%s enabled=%s lat=%s lon=%s use_sensor=%s", threshold, phone, enabled, lat, lon, use_sensor)
    return get_alert_config()

def set_alert_active(active, last_sent_iso=None):
    conn = get_conn()
    cur = conn.cursor()
    now = datetime.utcnow().isoformat()
    if active:
        # Update last_sent only when we activate the alert (and SMS is sent)
        cur.execute("UPDATE alert_config SET active=1, last_sent=?, updated_at=? WHERE id=(SELECT id FROM alert_config ORDER BY id DESC LIMIT 1)", (last_sent_iso or now, now))
    else:
        # Clear active status when risk drops
        cur.execute("UPDATE alert_config SET active=0, updated_at=? WHERE id=(SELECT id FROM alert_config ORDER BY id DESC LIMIT 1)", (now,))
    conn.commit()
    conn.close()

# ---------- SERIAL (background reader) ----------
def open_serial():
    global _serial_obj
    if serial is None:
        logging.info("pyserial not installed or unavailable; serial disabled.")
        return
    try:
        _serial_obj = serial.Serial(SERIAL_PORT, SERIAL_BAUD, timeout=1)
        time.sleep(2)
        logging.info("✔ Arduino serial opened on %s", SERIAL_PORT)
    except Exception as e:
        logging.error("Could not open serial %s: %s", SERIAL_PORT, e)
        _serial_obj = None

def serial_reader_loop():
    global _last_live, _last_live_ts
    logging.info("Serial reader thread starting")

    is_serial_active = serial and _serial_obj
    mock_val = None

    if not is_serial_active:
        logging.warning("Running in MOCK SENSOR mode: Generating random water level data.")
        mock_val = random.uniform(20.0, 80.0)

    while not _stop_event.is_set():
        if is_serial_active:
            # --- REAL SERIAL READING LOGIC ---
            if _serial_obj:
                try:
                    if _serial_obj.in_waiting > 0:
                        raw = _serial_obj.readline().decode(errors="ignore").strip()
                        if not raw:
                            continue
                        s = raw.upper()
                        for token in ("LIVE:", "DATA:", "DIST:", "D:"):
                            if s.startswith(token):
                                s = s[len(token):]
                                break
                        s = s.replace("CM", "").strip()
                        try:
                            val = float(s)
                            with _last_live_lock:
                                _last_live = float(val)
                                _last_live_ts = datetime.utcnow().isoformat()
                        except Exception:
                            logging.warning("Failed to parse serial data: raw='%s', parsed_s='%s'", raw, s)
                            pass
                except SerialException as se:
                    logging.error("SerialException: %s", se)
                except Exception as e:
                    logging.error("Serial read error: %s", e)
            time.sleep(0.2)
        else:
            # --- MOCK DATA LOGIC (for when serial is unavailable) ---
            if mock_val is None:
                mock_val = random.uniform(20.0, 80.0)

            change = random.uniform(-1.0, 1.0) * random.random()
            mock_val += change

            mock_val = max(10.0, min(90.0, mock_val))

            with _last_live_lock:
                _last_live = round(mock_val, 2)
                _last_live_ts = datetime.utcnow().isoformat()

            time.sleep(1)

    logging.info("Serial reader exiting")

# ---------- PERIODIC SAVER ----------
def periodic_saver_loop():
    logging.info("Periodic saver thread starting")
    while not _stop_event.is_set():
        time.sleep(SAVED_EVERY_SECONDS)
        with _last_live_lock:
            val = _last_live
            ts = _last_live_ts
        if val is not None:
            try:
                insert_water(val, ts)
                logging.info("Saved sensor value %.3f cm at %s", val, ts)
            except Exception as e:
                logging.error("Failed to save periodic sensor value: %s", e)
    logging.info("Periodic saver exiting")

# ---------- FLOOD RISK CALC ----------
def compute_flood_risk(lat, lon, use_sensor=False):
    """
    Compute flood risk.
    - If use_sensor == True, include the sensor's latest distance in the risk score.
    - If use_sensor == False, compute using only weather.
    """
    # weather fetch
    try:
        r = requests.get(
            f"https://api.openweathermap.org/data/2.5/weather?lat={lat}&lon={lon}&appid={OPENWEATHER_API_KEY}&units=metric",
            timeout=8
        )
        w = r.json()
    except Exception as e:
        logging.error("Weather request failed: %s", e)
        return {"error": "weather_failed"}

    clouds = float(w.get("clouds", {}).get("all", 0))
    wind = float(w.get("wind", {}).get("speed", 0))
    cloud_factor = clouds / 100.0
    wind_factor = min(1.0, wind / 30.0)

    water_factor = 0.0
    latest_distance = None

    # Sensor data is only used when explicitly requested (use_sensor=True)
    if use_sensor:
        with _last_live_lock:
            live = _last_live
        if live is None:
            db_latest = fetch_latest_row()
            if db_latest:
                latest_distance = float(db_latest["distance"])
        else:
            latest_distance = float(live)

        if latest_distance is not None:
            MIN_D = 10.0  # close = high water (10 cm)
            MAX_D = 100.0 # far = low water (100 cm)
            # invert distance to water factor: lower distance => higher water_factor
            if latest_distance <= MIN_D:
                water_factor = 1.0
            elif latest_distance >= MAX_D:
                water_factor = 0.0
            else:
                water_factor = (MAX_D - latest_distance) / (MAX_D - MIN_D)

    # Weighting logic
    if use_sensor and latest_distance is not None:
        # Live location: sensor dominates but weather contributes
        risk_score = (0.70 * water_factor) + (0.20 * cloud_factor) + (0.10 * wind_factor)
    else:
        # Other locations or sensor unavailable: weather-only
        risk_score = (0.70 * cloud_factor) + (0.30 * wind_factor)

    risk_percent = round(risk_score * 100.0, 1)
    severity = "Low"
    if risk_percent >= 75:
        severity = "High"
    elif risk_percent >= 40:
        severity = "Moderate"

    return {
        "risk_percent": risk_percent,
        "severity": severity,
        "latest_distance": latest_distance if use_sensor else None,
        "sensor_used": bool(use_sensor and latest_distance is not None),
        "weather_description": w.get("weather", [{}])[0].get("description", ""),
        "components": {
            "water_factor": round(water_factor * 100.0, 1),
            "cloud_factor": round(cloud_factor * 100.0, 1),
            "wind_factor": round(wind_factor * 100.0, 1)
        }
    }

# ---------- PREDICTION (simple linear fit) ----------
def linear_predict(minutes=60, points=30):
    rows = fetch_recent(points)
    if not rows or len(rows) < 2:
        return {"error": "not_enough_data"}
    xs = []
    ys = []
    for r in reversed(rows):
        try:
            ts = datetime.fromisoformat(r["timestamp"])
        except Exception:
            ts = datetime.utcnow()
        xs.append(ts.timestamp())
        ys.append(float(r["distance"]))
    n = len(xs)
    mean_x = sum(xs)/n
    mean_y = sum(ys)/n
    num = sum((xs[i]-mean_x)*(ys[i]-mean_y) for i in range(n))
    den = sum((xs[i]-mean_x)**2 for i in range(n))
    slope = num/den if den != 0 else 0.0
    intercept = mean_y - slope*mean_x
    future_x = xs[-1] + minutes*60
    predicted = slope*future_x + intercept
    return {"predicted_value": round(float(predicted),2), "slope_per_min": slope*60.0}

# ---------- TWILIO SENDER ----------
def send_sms(to_number, message):
    if TwilioClient is None:
        logging.warning("Twilio client not installed; cannot send SMS.")
        return False, "twilio_missing"
    try:
        client = TwilioClient(TWILIO_SID, TWILIO_TOKEN)
        sms = client.messages.create(to=to_number, from_=TWILIO_FROM, body=message)
        logging.info("Sent SMS to %s sid=%s. Status: %s", to_number, getattr(sms,"sid", None), getattr(sms,"status", None))
        return True, getattr(sms,"sid", None)
    except TwilioRestException as tre:
        logging.error("Twilio REST Error (Code %s): %s", getattr(tre, "status", None), tre)
        return False, f"Twilio REST Error: {getattr(tre, 'status', None)}"
    except Exception as e:
        logging.error("Twilio send failed (Check SID/TOKEN, FROM number, and TO number/verification): %s", e)
        return False, str(e)

# ---------- ALERT MONITOR (server-side) ----------
def alert_monitor_loop():
    logging.info("Alert monitor thread started (every %ds)", ALERT_CHECK_SECONDS)
    while not _stop_event.is_set():
        try:
            cfg = get_alert_config()
            if cfg and cfg.get("enabled"):
                lat = float(cfg.get("lat") or 0.0)
                lon = float(cfg.get("lon") or 0.0)
                threshold = float(cfg.get("threshold") or 0.0)
                phone = cfg.get("phone") or ""
                active = bool(cfg.get("active"))
                use_sensor_flag = bool(cfg.get("use_sensor"))

                # Compute risk using the alert-configured use_sensor flag
                risk = compute_flood_risk(lat, lon, use_sensor=use_sensor_flag)
                if risk.get("error"):
                    logging.debug("Alert monitor: weather failed")
                else:
                    rp = float(risk.get("risk_percent", 0))
                    logging.debug("Alert monitor: risk=%s threshold=%s active=%s use_sensor=%s", rp, threshold, active, use_sensor_flag)

                    # trigger on crossing (below->above)
                    if rp >= threshold and not active:
                        msg = f"Auto Flood Alert: risk {rp}% ({risk.get('severity')}). Latest distance: {risk.get('latest_distance') or 'N/A'} cm. Location: {lat},{lon}"

                        current_sent_time = datetime.utcnow().isoformat()

                        if phone:
                            ok, info = send_sms(phone, msg)
                            if ok:
                                set_alert_active(True, last_sent_iso=current_sent_time)
                                logging.info("Alert activated and SMS sent.")
                            else:
                                # still set active to avoid repeated failures; log failure
                                set_alert_active(True, last_sent_iso=current_sent_time)
                                logging.warning("SMS failed but alert set active: %s", info)
                        else:
                            set_alert_active(True, last_sent_iso=current_sent_time)
                            logging.info("Alert active (no phone configured)")
                    elif rp < threshold and active:
                        set_alert_active(False)
                        logging.info("Alert reset (risk dropped below threshold)")
            # else: no config or disabled
        except Exception as e:
            logging.error("Alert monitor exception: %s", e)
        time.sleep(ALERT_CHECK_SECONDS)
    logging.info("Alert monitor exiting")

# ---------- FLASK ROUTES ----------
@app.route("/")
def home():
    return render_template("index.html")

@app.route("/distance")
def distance_route():
    # Return live value from memory if present, else DB latest
    with _last_live_lock:
        val = _last_live
        ts = _last_live_ts
    if val is not None:
        return jsonify({"distance": val, "timestamp": ts, "source": "memory"})
    latest = fetch_latest_row()
    if latest:
        return jsonify({"distance": latest["distance"], "timestamp": latest["timestamp"], "source": "db"})
    return jsonify({"distance": None, "source": "none"})

@app.route("/recent_water")
def recent_water_route():
    rows = fetch_recent(30)
    return jsonify(rows)

@app.route("/weather_by_coords", methods=["POST"])
def weather_by_coords_route():
    body = request.json or {}
    lat = body.get("lat")
    lon = body.get("lon")
    if lat is None or lon is None:
        return jsonify({"error": "lat/lon required"}), 400
    try:
        r = requests.get(f"https://api.openweathermap.org/data/2.5/weather?lat={lat}&lon={lon}&appid={OPENWEATHER_API_KEY}&units=metric", timeout=8)
        return jsonify(r.json())
    except Exception as e:
        logging.error("weather_by_coords error: %s", e)
        return jsonify({"error": "weather_fetch_failed"}), 503

@app.route("/flood_risk", methods=["POST"])
def flood_risk_route():
    body = request.json or {}
    lat = body.get("lat")
    lon = body.get("lon")
    if lat is None or lon is None:
        return jsonify({"error": "lat/lon required"}), 400

    # Frontend should explicitly pass use_sensor=True for live location
    use_sensor = bool(body.get("use_sensor", False))

    res = compute_flood_risk(float(lat), float(lon), use_sensor)
    if res.get("error"):
        return jsonify(res), 503
    return jsonify(res)

@app.route("/send_alert", methods=["POST"])
def send_alert_route():
    body = request.json or {}
    to = body.get("to")
    msg = body.get("message")
    if not to or not msg:
        return jsonify({"error": "to & message required"}), 400
    ok, info = send_sms(to, msg)
    if ok:
        return jsonify({"status": "sent", "info": info})
    return jsonify({"error": info}), 500

# Alert config endpoints
@app.route("/alert_config", methods=["GET", "POST"])
def alert_config_route():
    if request.method == "GET":
        cfg = get_alert_config()
        if not cfg:
            return jsonify({"exists": False})
        return jsonify({"exists": True, "config": cfg})
    else:
        body = request.json or {}
        threshold = float(body.get("threshold", 0))
        phone = body.get("phone", "") or ""
        enabled = bool(body.get("enabled", False))
        lat = float(body.get("lat", 0.0))
        lon = float(body.get("lon", 0.0))
        # Frontend can pass use_sensor True/False when saving alert config
        use_sensor = bool(body.get("use_sensor", False))
        cfg = save_alert_config(threshold, phone, enabled, lat, lon, use_sensor)
        return jsonify({"status": "saved", "config": cfg})

@app.route("/alert_status")
def alert_status_route():
    cfg = get_alert_config()
    if not cfg:
        return jsonify({"exists": False})
    rp = None
    try:
        if cfg.get("enabled"):
            lat = float(cfg.get("lat"))
            lon = float(cfg.get("lon"))
            use_sensor_flag = bool(cfg.get("use_sensor"))
            risk = compute_flood_risk(lat, lon, use_sensor=use_sensor_flag)
            if not risk.get("error"):
                rp = risk.get("risk_percent")
    except Exception as e:
        logging.error("alert_status compute error: %s", e)

    # Send last_sent value to the frontend for tracking
    last_sent_time = cfg.get("last_sent") if cfg.get("active") == 1 else None

    return jsonify({"exists": True, "config": cfg, "current_risk": rp, "last_sent_time": last_sent_time})

@app.route("/predict", methods=["POST"])
def predict_route():
    minutes = int(request.json.get("minutes", 60)) if request.json else 60
    return jsonify(linear_predict(minutes=minutes, points=30))

@app.route("/heatmap", methods=["POST"])
def heatmap_route():
    body = request.json or {}
    lat = float(body.get("lat", 11.0))
    lon = float(body.get("lon", 77.0))
    radius_km = float(body.get("radius_km", 5.0))
    points = int(body.get("points", 120))

    # HEATMAP: Use weather-only risk calculation for general area (use_sensor=False)
    base = compute_flood_risk(lat, lon, use_sensor=False)
    base_risk = (base.get("risk_percent", 50)/100.0) if not base.get("error") else 0.5
    out = []
    max_deg = radius_km / 111.0
    for _ in range(points):
        r = random.random() ** 0.5 * max_deg
        t = random.random() * math.pi * 2
        dlat = r * math.cos(t)
        dlon = r * math.sin(t) / max(0.00001, math.cos(math.radians(lat)))
        plat = lat + dlat
        plon = lon + dlon
        dist_factor = max(0.0, 1.0 - (r / max_deg))
        noise = random.uniform(0.85, 1.15)
        intensity = max(0.0, min(1.0, base_risk * dist_factor * noise))
        out.append({"lat": plat, "lon": plon, "intensity": round(float(intensity),3)})
    return jsonify(out)

# ---------- START & THREADS ----------
def start_background():
    # open serial if available
    open_serial()
    # threads
    t = threading.Thread(target=serial_reader_loop, daemon=True)
    t.start()
    saver = threading.Thread(target=periodic_saver_loop, daemon=True)
    saver.start()
    monitor = threading.Thread(target=alert_monitor_loop, daemon=True)
    monitor.start()
    logging.info("Background threads started")

if __name__ == "__main__":
    init_db()
    start_background()
    # debug off recommended; reloader disabled because we run background threads
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
