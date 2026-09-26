import os
import sys
import socket
import threading
import requests
import time
from datetime import datetime, timezone
from functools import wraps
from collections import Counter

from flask import Flask, request, render_template, redirect, url_for, session, flash, jsonify
from pymongo import MongoClient
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
import certifi
from bson.objectid import ObjectId

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'default_dev_key')
app.config['TEMPLATES_AUTO_RELOAD'] = True

# ==========================================
# Database Configuration (MongoDB Atlas)
# ==========================================
MONGO_URI = os.getenv('MONGO_URI')
MONGO_DB = os.getenv('MONGO_DB', 'cattle_monitoring')

try:
    client = MongoClient(MONGO_URI, tlsCAFile=certifi.where())
    db = client[MONGO_DB]
    client.admin.command('ping')
    print("Successfully connected to MongoDB Atlas!")
except Exception as e:
    print(f"Error connecting to MongoDB: {e}")
    sys.exit(1)

# Collections
users_col = db['users']
farms_col = db['farms']
cattle_col = db['cattle']
vaccinations_col = db['vaccinations']
estrus_col = db['estrus']
alerts_col = db['alerts']
settings_col = db['settings']

# Global in-memory pointer for instant RFID scanning redirect
latest_rfid_scan_event = {"cattle_id": None, "timestamp": 0}

def notify(title, msg, user_ids):
    app_id = os.getenv("ONESIGNAL_APP_ID")
    api_key = os.getenv("ONESIGNAL_REST_API_KEY")

    if not app_id or not api_key:
        print("[OneSignal Error] Missing ONESIGNAL_APP_ID or ONESIGNAL_REST_API_KEY in .env!")
        return

    if isinstance(user_ids, str):
        user_ids = [user_ids]

    payload = {
        "app_id": app_id,
        "include_aliases": {
            "external_id": user_ids
        },
        "headings": {"en": title},
        "contents": {"en": msg},
        "target_channel": "push"
    }

    try:
        res = requests.post(
            "https://onesignal.com/api/v1/notifications",
            headers={
                "Authorization": f"Key {api_key}",  # Note: OneSignal REST API v16 uses 'Key <token>' or 'Basic <token>'
                "Content-Type": "application/json; charset=utf-8"
            },
            json=payload,
            timeout=5
        )
        print(f"[OneSignal API Response {res.status_code}]: {res.text}")
    except Exception as e:
        print(f"[OneSignal Request Failed]: {e}")

# ==========================================
# Background Workers (Discovery & Timeout)
# ==========================================
def udp_discovery_beacon():
    """Broadcasts Flask server IP across local subnet every 3 seconds."""
    beacon_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    beacon_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    while True:
        try:
            beacon_socket.sendto(b"AGRISAAS_BEACON:5000", ('<broadcast>', 4210))
        except Exception as e:
            print(f"Beacon error: {e}")
        time.sleep(3)

def presence_timeout_worker():
    """Marks cattle as absent if no telemetry/scan ping received within timeout window."""
    while True:
        try:
            settings = settings_col.find_one() or {"presence_timeout_minutes": 1}
            timeout_seconds = int(settings.get("presence_timeout_minutes", 1)) * 60
            now = datetime.now(timezone.utc)

            for c in cattle_col.find({"presence_status": "present"}):
                last_seen_str = c.get('last_seen')
                if last_seen_str:
                    try:
                        last_seen_dt = datetime.fromisoformat(last_seen_str.replace('Z', '+00:00'))
                        if (now - last_seen_dt).total_seconds() > timeout_seconds:
                            cattle_col.update_one(
                                {"_id": c['_id']},
                                {"$set": {"presence_status": "absent", "updated_at": now.isoformat()}}
                            )
                    except Exception:
                        pass
        except Exception as e:
            print(f"Presence worker error: {e}")
        time.sleep(15)

threading.Thread(target=udp_discovery_beacon, daemon=True).start()
threading.Thread(target=presence_timeout_worker, daemon=True).start()

# ==========================================
# Security Decorators
# ==========================================
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session or session.get('role') != 'admin':
            flash("Unauthorized access. Admin privileges required.", "danger")
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def farmer_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session or session.get('role') != 'farmer':
            flash("Please log in as a farmer to view this page.", "danger")
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# ==========================================
# Hardware APIs
# ==========================================
@app.route('/api/scan', methods=['POST'])
def api_scan():
    """RFID Chute Scanner Route: Triggers web UI navigation only."""
    global latest_rfid_scan_event, latest_data_version
    data = request.get_json(force=True, silent=True)
    if not data or 'rfid_uid' not in data:
        return jsonify({"found": False, "message": "Missing RFID UID"}), 400

    rfid_uid = data.get('rfid_uid').strip().upper()
    cattle = cattle_col.find_one({"rfid_uid": rfid_uid})
    if not cattle:
        return jsonify({"found": False, "message": "RFID not registered"}), 404

    cattle_id = cattle['cattle_id']
    now_str = datetime.now(timezone.utc).isoformat()

    # Log chute scan event without modifying live presence_status
    cattle_col.update_one(
        {"_id": cattle['_id']},
        {"$set": {
            "last_chute_scan": now_str,
            "last_scanner_location": data.get('location', 'Main Chute Scanner')
        }}
    )

    # Trigger UI auto-redirect event
    latest_rfid_scan_event = {
        "cattle_id": cattle_id,
        "timestamp": time.time()
    }
    latest_data_version = time.time()

    cattle.pop('_id', None)
    return jsonify({"found": True, "cattle": cattle}), 200

@app.route('/api/telemetry', methods=['POST'])
def receive_telemetry():
    """NodeMCU Belt Route (Updates temp & presence ONLY, no page redirects)"""
    data = request.get_json(force=True, silent=True)
    if not data or 'cattle_id' not in data:
        return jsonify({"status": "error", "message": "Missing cattle_id"}), 400

    cattle_id = str(data.get('cattle_id')).strip().upper()
    temperature = float(data.get('temperature', 38.5))
    zone = data.get('location', 'Pasture Sector A')
    now_iso = datetime.now(timezone.utc).isoformat()

    result = cattle_col.update_one(
        {"cattle_id": cattle_id},
        {"$set": {
            "presence_status": "present",
            "last_seen": now_iso,
            "last_location": zone,
            "last_temperature": temperature,
            "updated_at": now_iso
        }}
    )

    if result.matched_count == 0:
        return jsonify({"status": "error", "message": "Cattle ID not found"}), 404

    if temperature > 35:
        alerts_col.insert_one({
            "cattle_id": cattle_id,
            "type": "health",
            "message": f"High fever detected: {temperature}°C in {zone}",
            "status": "unread",
            "created_at": now_iso
        })

    if temperature > 35:
        msg = f"High temperature alert: {temperature}°C on cow {cattle_id}"
        alerts_col.insert_one({
            "cattle_id": cattle_id,
            "type": "health",
            "message": msg,
            "status": "unread",
            "created_at": now_iso
        })
        notify(f"⚠️ Temp Alert: {cattle_id}", msg, ["cattle", "cattle_1", "cattle_2", "cattle_3"])

    return jsonify({"status": "success", "cattle_id": cattle_id, "presence": "present"}), 200

@app.route('/api/latest_scan')
@farmer_required
def latest_scan():
    """Poll endpoint used by web frontend for live auto-navigation"""
    current_time = time.time()
    # If a card was scanned within the last 3.5 seconds
    if latest_rfid_scan_event["cattle_id"] and (current_time - latest_rfid_scan_event["timestamp"]) < 3.5:
        return jsonify({
            "status": "found",
            "cattle_id": latest_rfid_scan_event["cattle_id"],
            "timestamp": latest_rfid_scan_event["timestamp"]
        })
    return jsonify({"status": "waiting"})

# ==========================================
# Web Routes
# ==========================================
@app.route('/')
def index():
    if 'user_id' in session:
        if session.get('role') == 'admin':
            return redirect(url_for('admin_dashboard'))
        return redirect(url_for('farmer_dashboard'))
    return redirect(url_for('login'))

# ==========================================
# Admin Routes
# ==========================================
@app.route('/admin')
@admin_required
def admin_dashboard():
    total_farmers = users_col.count_documents({"role": "farmer"})
    total_farms = farms_col.count_documents({})
    total_cattle = cattle_col.count_documents({})
    return render_template(
        'admin_dashboard.html',
        total_farmers=total_farmers,
        total_farms=total_farms,
        total_cattle=total_cattle
    )

@app.route('/admin/farmers')
@admin_required
def admin_farmers():
    farmers = list(users_col.find({"role": "farmer"}))
    farms = list(farms_col.find())
    return render_template('admin_farmers.html', farmers=farmers, farms=farms)

@app.route('/admin/assign_farms/<farmer_id>', methods=['POST'])
@admin_required
def assign_farms(farmer_id):
    selected_farm_ids = request.form.getlist('farm_ids')
    users_col.update_one(
        {"_id": ObjectId(farmer_id)},
        {"$set": {"farm_ids": selected_farm_ids}}
    )
    flash("Farm assignments updated successfully.", "success")
    return redirect(url_for('admin_farmers'))

@app.route('/admin/farms', methods=['GET', 'POST'])
@admin_required
def admin_farms():
    if request.method == 'POST':
        farm_id = request.form.get('farm_id', '').strip().upper()
        name = request.form.get('name', '').strip()
        location = request.form.get('location', '').strip()

        if farms_col.find_one({"farm_id": farm_id}):
            flash("Farm ID already exists. Please choose a unique ID.", "danger")
        else:
            farms_col.insert_one({
                "farm_id": farm_id,
                "name": name,
                "location": location,
                "created_at": datetime.now(timezone.utc).isoformat()
            })
            flash(f"Farm '{name}' created successfully.", "success")
        return redirect(url_for('admin_farms'))

    farms = list(farms_col.find())
    return render_template('admin_farms.html', farms=farms)

@app.route('/admin/settings', methods=['GET', 'POST'])
@admin_required
def admin_settings():
    user = users_col.find_one({"_id": ObjectId(session['user_id'])})
    settings = settings_col.find_one() or {
        "estrus_alert_days": 2,
        "vaccination_alert_days": 2,
        "presence_timeout_minutes": 1
    }

    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'update_thresholds':
            try:
                estrus_days = int(request.form.get('estrus_alert_days', 2))
                vax_days = int(request.form.get('vaccination_alert_days', 2))
                presence_timeout = int(request.form.get('presence_timeout_minutes', 1))
                settings_col.update_one(
                    {},
                    {"$set": {
                        "estrus_alert_days": estrus_days,
                        "vaccination_alert_days": vax_days,
                        "presence_timeout_minutes": presence_timeout
                    }},
                    upsert=True
                )
                flash("System thresholds updated.", "success")
            except Exception:
                flash("Invalid values provided.", "danger")
        elif action == 'update_profile':
            name = request.form.get('name', '').strip()
            email = request.form.get('email', '').strip()
            users_col.update_one({"_id": user['_id']}, {"$set": {"name": name, "email": email}})
            session['name'] = name
            flash("Profile updated.", "success")
        elif action == 'change_password':
            current_pw = request.form.get('current_password', '')
            new_pw = request.form.get('new_password', '')
            if not check_password_hash(user.get('password_hash', ''), current_pw):
                flash("Current password incorrect.", "danger")
            elif len(new_pw) < 6:
                flash("Password must be at least 6 characters.", "danger")
            else:
                users_col.update_one({"_id": user['_id']}, {"$set": {"password_hash": generate_password_hash(new_pw)}})
                flash("Password changed successfully.", "success")

        return redirect(url_for('admin_settings'))

    return render_template('farmer_settings.html', user=user, settings=settings)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        user = users_col.find_one({"email": email, "active": True})

        if user and check_password_hash(user['password_hash'], password):
            session['user_id'] = str(user['_id'])
            session['role'] = user['role']
            session['name'] = user['name']
            flash(f"Welcome back, {user['name']}!", "success")
            if user['role'] == 'admin':
                return redirect(url_for('admin_dashboard'))
            return redirect(url_for('farmer_dashboard'))

        flash("Invalid email or password.", "danger")
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name = request.form.get('name')
        email = request.form.get('email')
        password = request.form.get('password')

        if users_col.find_one({"email": email}):
            flash("That email is already registered. Please log in.", "danger")
            return redirect(url_for('register'))

        users_col.insert_one({
            "name": name,
            "email": email,
            "password_hash": generate_password_hash(password),
            "role": "farmer",
            "farm_ids": [],
            "active": True,
            "created_at": datetime.now(timezone.utc).isoformat()
        })
        flash("Registration successful! Please log in.", "success")
        return redirect(url_for('login'))

    return render_template('register.html')

@app.route('/logout')
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for('login'))

@app.route('/farmer')
@farmer_required
def farmer_dashboard():
    user = users_col.find_one({"_id": ObjectId(session['user_id'])})
    farm_ids = user.get('farm_ids', [])
    assigned_farms = []

    for farm in farms_col.find({"farm_id": {"$in": farm_ids}}):
        total_cattle = cattle_col.count_documents({"farm_id": farm['farm_id']})
        present_cattle = cattle_col.count_documents({"farm_id": farm['farm_id'], "presence_status": "present"})
        farm['total_cattle'] = total_cattle
        farm['present_cattle'] = present_cattle
        farm['absent_cattle'] = total_cattle - present_cattle
        assigned_farms.append(farm)

    return render_template('farmer_dashboard.html', farms=assigned_farms)

@app.route('/farmer/farm/<farm_id>')
@farmer_required
def view_farm(farm_id):
    user = users_col.find_one({"_id": ObjectId(session['user_id'])})
    if farm_id not in user.get('farm_ids', []):
        flash("Unauthorized access to this farm.", "danger")
        return redirect(url_for('farmer_dashboard'))

    farm = farms_col.find_one({"farm_id": farm_id})
    cattle_list = list(cattle_col.find({"farm_id": farm_id}))
    return render_template('farm_view.html', farm=farm, cattle_list=cattle_list)

@app.route('/farmer/cattle/<cattle_id>')
@farmer_required
def view_cattle(cattle_id):
    cattle = cattle_col.find_one({"cattle_id": cattle_id})
    if not cattle:
        flash("Cattle not found.", "danger")
        return redirect(url_for('farmer_dashboard'))

    user = users_col.find_one({"_id": ObjectId(session['user_id'])})
    if cattle['farm_id'] not in user.get('farm_ids', []):
        flash("Unauthorized access.", "danger")
        return redirect(url_for('farmer_dashboard'))

    vaccinations = list(vaccinations_col.find({"cattle_id": cattle_id}))
    estrus = estrus_col.find_one({"cattle_id": cattle_id})
    settings = settings_col.find_one() or {"estrus_alert_days": 2, "vaccination_alert_days": 2}

    # --- Estrus Alert Check & Auto-Clearing ---
    if estrus and estrus.get('expected_next_date'):
        threshold_days = int(settings.get("estrus_alert_days", 2))
        
        try:
            expected_date = datetime.fromisoformat(estrus['expected_next_date']).replace(tzinfo=timezone.utc)
            days_until = (expected_date - datetime.now(timezone.utc)).days
            
            if 0 <= days_until <= threshold_days:
                # Within threshold: ensure active unread alert exists
                existing_alert = alerts_col.find_one({
                    "cattle_id": cattle_id,
                    "type": "estrus",
                    "status": "unread"
                })
                if not existing_alert:
                    alert_msg = f"Cattle {cattle['name']} ({cattle_id}) estrus is expected in {days_until} day(s)."
                    alerts_col.insert_one({
                        "cattle_id": cattle_id,
                        "farm_id": cattle['farm_id'],
                        "type": "estrus",
                        "message": alert_msg,
                        "status": "unread",
                        "created_at": datetime.now(timezone.utc).isoformat()
                    })
                    notify(f"⚠️ Estrus Alert: {cattle['name']}", alert_msg, ["cattle", "cattle_1", "cattle_2", "cattle_3"])
            else:
                # Date has passed (days_until < 0) or is too far in future: clear previous unread alerts
                alerts_col.update_many(
                    {
                        "cattle_id": cattle_id,
                        "type": "estrus",
                        "status": "unread"
                    },
                    {
                        "$set": {
                            "status": "resolved",
                            "resolved_at": datetime.now(timezone.utc).isoformat()
                        }
                    }
                )
        except ValueError:
            pass

    # --- Vaccination Alert Check & Auto-Clearing ---
    vax_threshold_days = int(settings.get("vaccination_alert_days", 2))
    for vax in vaccinations:
        if vax.get('next_due_date'):
            try:
                due_date = datetime.fromisoformat(vax['next_due_date']).replace(tzinfo=timezone.utc)
                vax_days_until = (due_date - datetime.now(timezone.utc)).days
                vax_name = vax.get('vaccine_name', 'Vaccination')

                if 0 <= vax_days_until <= vax_threshold_days:
                    existing_vax_alert = alerts_col.find_one({
                        "cattle_id": cattle_id,
                        "type": "vaccination",
                        "vaccine_name": vax_name,
                        "status": "unread"
                    })
                    if not existing_vax_alert:
                        vax_alert_msg = f"Cattle {cattle['name']} ({cattle_id}) {vax_name} vaccination is due in {vax_days_until} day(s)."
                        alerts_col.insert_one({
                            "cattle_id": cattle_id,
                            "farm_id": cattle['farm_id'],
                            "type": "vaccination",
                            "vaccine_name": vax_name,
                            "message": vax_alert_msg,
                            "status": "unread",
                            "created_at": datetime.now(timezone.utc).isoformat()
                        })
                        notify(f"⚠️ Vaccine Alert: {cattle['name']}", vax_alert_msg, ["cattle", "cattle_1", "cattle_2", "cattle_3"])
                else:
                    alerts_col.update_many(
                        {
                            "cattle_id": cattle_id,
                            "type": "vaccination",
                            "vaccine_name": vax_name,
                            "status": "unread"
                        },
                        {
                            "$set": {
                                "status": "resolved",
                                "resolved_at": datetime.now(timezone.utc).isoformat()
                            }
                        }
                    )
            except ValueError:
                pass

    active_alerts = list(alerts_col.find({"cattle_id": cattle_id, "status": "unread"}))

    return render_template('cattle_details.html',
                           cattle=cattle,
                           vaccinations=vaccinations,
                           estrus=estrus,
                           alerts=active_alerts)

@app.route('/farmer/farm/<farm_id>/add_cattle', methods=['GET', 'POST'])
@farmer_required
def add_cattle(farm_id):
    user = users_col.find_one({"_id": ObjectId(session['user_id'])})
    if farm_id not in user.get('farm_ids', []):
        flash("Unauthorized access to this farm.", "danger")
        return redirect(url_for('farmer_dashboard'))

    if request.method == 'POST':
        cattle_id = request.form.get('cattle_id').strip().upper()
        rfid_uid = request.form.get('rfid_uid').strip().upper()

        if cattle_col.find_one({"cattle_id": cattle_id}):
            flash("Cattle ID already exists. Please use a unique ID.", "danger")
            return redirect(request.url)
        if cattle_col.find_one({"rfid_uid": rfid_uid}):
            flash("RFID UID is already registered to another cow.", "danger")
            return redirect(request.url)

        new_cattle = {
            "cattle_id": cattle_id,
            "farm_id": farm_id,
            "rfid_uid": rfid_uid,
            "name": request.form.get('name'),
            "breed": request.form.get('breed'),
            "gender": request.form.get('gender'),
            "date_of_birth": request.form.get('date_of_birth'),
            "weight": float(request.form.get('weight', 0) or 0),
            "health_status": request.form.get('health_status', 'healthy'),
            "presence_status": "absent",  # Defaults to absent until belt or scanner pings
            "last_temperature": None,
            "last_seen": None,
            "notes": request.form.get('notes', ''),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat()
        }

        cattle_col.insert_one(new_cattle)
        flash(f"Cattle {new_cattle['name']} added successfully!", "success")
        return redirect(url_for('view_farm', farm_id=farm_id))

    return render_template('add_cattle.html', farm_id=farm_id)

@app.route('/farmer/settings', methods=['GET', 'POST'])
@farmer_required
def farmer_settings():
    user = users_col.find_one({"_id": ObjectId(session['user_id'])})
    settings = settings_col.find_one() or {
        "estrus_alert_days": 2,
        "vaccination_alert_days": 2,
        "presence_timeout_minutes": 1
    }

    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'update_profile':
            name = request.form.get('name', '').strip()
            email = request.form.get('email', '').strip()
            existing_user = users_col.find_one({"email": email, "_id": {"$ne": user['_id']}})
            if existing_user:
                flash("Email is already in use.", "danger")
            else:
                users_col.update_one({"_id": user['_id']}, {"$set": {"name": name, "email": email}})
                session['name'] = name
                flash("Profile details updated successfully.", "success")

        elif action == 'update_thresholds':
            try:
                estrus_days = int(request.form.get('estrus_alert_days', 2))
                vax_days = int(request.form.get('vaccination_alert_days', 2))
                presence_timeout = int(request.form.get('presence_timeout_minutes', 30))
                settings_col.update_one(
                    {},
                    {"$set": {
                        "estrus_alert_days": estrus_days,
                        "vaccination_alert_days": vax_days,
                        "presence_timeout_minutes": presence_timeout
                    }},
                    upsert=True
                )
                flash("System thresholds updated.", "success")
            except Exception:
                flash("Invalid numbers provided.", "danger")

        elif action == 'change_password':
            current_pw = request.form.get('current_password', '')
            new_pw = request.form.get('new_password', '')
            if not check_password_hash(user.get('password_hash', ''), current_pw):
                flash("Current password is incorrect.", "danger")
            elif len(new_pw) < 6:
                flash("New password must be at least 6 characters long.", "danger")
            else:
                users_col.update_one({"_id": user['_id']}, {"$set": {"password_hash": generate_password_hash(new_pw)}})
                flash("Password updated successfully.", "success")

        return redirect(url_for('farmer_settings'))

    return render_template('farmer_settings.html', user=user, settings=settings)

@app.route('/farmer/analytics')
@farmer_required
def farmer_analytics():
    user = users_col.find_one({"_id": ObjectId(session['user_id'])})
    farm_ids = user.get('farm_ids', [])
    cattle = list(cattle_col.find({"farm_id": {"$in": farm_ids}}))
    cattle_ids = [c['cattle_id'] for c in cattle]

    health_counts = dict(Counter([c.get('health_status', 'healthy').capitalize() for c in cattle]))
    breed_counts = dict(Counter([c.get('breed', 'Unknown') for c in cattle]))

    age_counts = {'Calf (<1 yr)': 0, 'Young (1-3 yrs)': 0, 'Adult (3-6 yrs)': 0, 'Senior (6+ yrs)': 0}
    now = datetime.now(timezone.utc)

    for c in cattle:
        dob_str = str(c.get('date_of_birth', ''))[:10]
        if dob_str:
            try:
                dob = datetime.strptime(dob_str, '%Y-%m-%d').replace(tzinfo=timezone.utc)
                age_years = (now - dob).days / 365.25
                if age_years < 1: age_counts['Calf (<1 yr)'] += 1
                elif age_years <= 3: age_counts['Young (1-3 yrs)'] += 1
                elif age_years <= 6: age_counts['Adult (3-6 yrs)'] += 1
                else: age_counts['Senior (6+ yrs)'] += 1
            except Exception:
                age_counts['Adult (3-6 yrs)'] += 1
        else:
            age_counts['Adult (3-6 yrs)'] += 1

    weight_counts = {'< 350 kg': 0, '350 - 450 kg': 0, '450 - 550 kg': 0, '> 550 kg': 0}
    for c in cattle:
        w = float(c.get('weight', 0) or 0)
        if w < 350: weight_counts['< 350 kg'] += 1
        elif w <= 450: weight_counts['350 - 450 kg'] += 1
        elif w <= 550: weight_counts['450 - 550 kg'] += 1
        else: weight_counts['> 550 kg'] += 1

    vax_records = list(vaccinations_col.find({"cattle_id": {"$in": cattle_ids}}))
    vax_type_counts = dict(Counter([v.get('vaccine_name', 'General') for v in vax_records]))
    if not vax_type_counts:
        vax_type_counts = {'FMD': 0, 'Brucellosis': 0, 'BVD': 0, 'Anthrax': 0}

    total_cattle = len(cattle)
    healthy_count = sum(1 for c in cattle if str(c.get('health_status', '')).lower() == 'healthy')
    health_score = round((healthy_count / total_cattle * 100), 1) if total_cattle > 0 else 100.0

    return render_template(
        'farmer_analytics.html',
        total_cattle=total_cattle,
        health_score=health_score,
        health_counts=health_counts,
        breed_counts=breed_counts,
        age_counts=age_counts,
        weight_counts=weight_counts,
        vax_type_counts=vax_type_counts
    )

@app.route('/farmer/alerts')
@farmer_required
def farmer_alerts():
    user = users_col.find_one({"_id": ObjectId(session['user_id'])})
    farm_ids = user.get('farm_ids', [])
    cattle_in_farms = [c['cattle_id'] for c in cattle_col.find({"farm_id": {"$in": farm_ids}})]
    active_alerts = list(alerts_col.find({"cattle_id": {"$in": cattle_in_farms}, "status": "unread"}).sort("created_at", -1))
    return render_template('alerts.html', alerts=active_alerts)

@app.route('/farmer/iot')
@farmer_required
def farmer_iot():
    return render_template('iot.html')

@app.route('/farmer/cattle/<cattle_id>/estrus', methods=['GET', 'POST'])
@farmer_required
def edit_estrus(cattle_id):
    cattle = cattle_col.find_one({"cattle_id": cattle_id})
    if request.method == 'POST':
        estrus_data = {
            "cattle_id": cattle_id,
            "last_estrus_date": request.form.get('last_estrus_date'),
            "expected_next_date": request.form.get('expected_next_date'),
            "cycle_length_days": int(request.form.get('cycle_length_days', 21)),
            "breeding_date": request.form.get('breeding_date') or None,
            "notes": request.form.get('notes', ''),
            "updated_at": datetime.now(timezone.utc).isoformat()
        }
        estrus_col.update_one({"cattle_id": cattle_id}, {"$set": estrus_data}, upsert=True)
        flash("Estrus record updated.", "success")
        return redirect(url_for('view_cattle', cattle_id=cattle_id))
    estrus = estrus_col.find_one({"cattle_id": cattle_id})
    return render_template('estrus_form.html', cattle=cattle, estrus=estrus)

@app.route('/farmer/cattle/<cattle_id>/vaccination', methods=['GET', 'POST'])
@farmer_required
def add_vaccination(cattle_id):
    cattle = cattle_col.find_one({"cattle_id": cattle_id})
    if request.method == 'POST':
        vax_data = {
            "cattle_id": cattle_id,
            "vaccine_name": request.form.get('vaccine_name'),
            "vaccination_date": request.form.get('vaccination_date'),
            "next_due_date": request.form.get('next_due_date'),
            "notes": request.form.get('notes', ''),
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        vaccinations_col.insert_one(vax_data)
        flash("Vaccine recorded.", "success")
        return redirect(url_for('view_cattle', cattle_id=cattle_id))
    return render_template('vaccination_form.html', cattle=cattle)

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)