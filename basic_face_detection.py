import cv2
import serial
import time
import sys
import os
import numpy as np
from deepface import DeepFace
import shutil
from datetime import datetime
from flask import Flask, render_template, Response, jsonify, request
from flask_socketio import SocketIO, emit
import threading
import base64
import json
from ultralytics import YOLO
import requests  # For direct Telegram messaging

# --- BACKGROUND FLASK SERVER ---
def start_flask_server():
    print("\n[🌐] Starting Flask Web Server...")
    print(f"[🌐] Access Dashboard at: http://localhost:5000")
    print(f"[🌐] To access remotely, use: http://10.29.49.14:5000")
    print("[💡 Your IP address is: 10.29.49.14]\n")
    socketio.run(app, host='0.0.0.0', port=5000, debug=False, use_reloader=False)

# --- CONFIGURATION ---
SERIAL_PORT = 'COM7'  # ⚠️ CHANGE TO YOUR ARDUINO PORT
BAUD_RATE = 9600
# YOLOv8 models are loaded below

# --- TELEGRAM CONFIGURATION ---
TELEGRAM_BOT_TOKEN = "8374895696:AAEm0Ulcy-zJxdNbXdJRx5sHn9mefbe4uVU"  # Same as ESP32
TELEGRAM_CHAT_ID = "6389631644"  # Same as ESP32

# --- GLOBALS ---
arduino = None
awaiting_user_decision = False  # Global flag for user decision
is_full_auto_mode = False
cap = None

# --- FLASK APP SETUP ---
app = Flask(__name__)
app.config['SECRET_KEY'] = 'nerf-turret-secret-key'
socketio = SocketIO(app, cors_allowed_origins="*")

# Global shared state (thread-safe)
shared_state = {
    "frame": None,
    "status": "CLEAR",
    "mode": "SEMI-AUTO",
    "faces": 0,
    "distance_front": 0,
    "distance_left": 0,
    "distance_right": 0,
    "temperature": 0.0,
    "battery": 0.0,
    "laser_on": False,
    "authorized_detected": False,
    "unauthorized_detected": False,
    "human_detected": False,
    "target_center": {"x": 0, "y": 0},
    "targets": [],
    "last_update": ""
}

state_lock = threading.Lock()

# Enrollment state
enrollment_mode = False
enrollment_name = ""
enrollment_count = 0
ENROLLMENT_STAGES = [
    "Look straight at camera 👀",
    "Turn head slightly LEFT ↖️",
    "Turn head slightly RIGHT ↗️",
    "Look UP a little ☝️",
    "Look DOWN a little 👇",
    "Smile naturally 😊",
    "Neutral expression 😐",
    "Blink or raise eyebrows 🤨",
    "Turn head further LEFT ⬅️",
    "Turn head further RIGHT ➡️"
]
ENROLLMENT_TARGET = len(ENROLLMENT_STAGES)

# Ensure directories exist
os.makedirs("dataset", exist_ok=True)
AUTHORIZED_DB_PATH = "authorized_db"
os.makedirs(AUTHORIZED_DB_PATH, exist_ok=True)

# --- LOAD DETECTION MODELS (YOLOv8) ---
print("[🔍] Loading YOLOv8 models...")

# Initialize YOLO models
person_model = None
face_model = None

try:
    # Load YOLOv8 models for person and face detection
    person_model = YOLO("yolov8n.pt")        # For detecting humans
    print("[✅] YOLOv8 person detection model loaded successfully.")
    
    face_model = YOLO("yolov8n-face.pt")     # For detecting faces
    print("[✅] YOLOv8 face detection model loaded successfully.")
except Exception as e:
    print(f"[❌] Failed to load YOLOv8 models: {e}")

# --- PRELOAD DEEPFACE MODEL ---
try:
    print("[🧠] Preloading DeepFace model...")
    dummy1 = np.zeros((224, 224, 3), dtype=np.uint8)
    dummy2 = np.zeros((224, 224, 3), dtype=np.uint8)
    DeepFace.verify(img1_path=dummy1, img2_path=dummy2, model_name="ArcFace",
                    enforce_detection=False, detector_backend="opencv", silent=True)
    print("[✅] DeepFace model preloaded successfully.")
except Exception as e:
    print(f"[⚠️] Failed to preload DeepFace model: {e}")

# --- DEEPFACE THROTTLING ---
last_recognition_time = {}
DEEPFACE_COOLDOWN = 1.0  # seconds

# --- VIDEO STREAM GENERATOR ---
def generate_frames():
    while True:
        with state_lock:
            frame = shared_state["frame"].copy() if shared_state["frame"] is not None else None
        if frame is not None:
            ret, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
            if ret:
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')
        else:
            blank = np.zeros((480, 640, 3), dtype=np.uint8)
            cv2.putText(blank, "No Camera Feed", (200, 240), cv2.FONT_HERSHEY_SIMPLEX, 1, (255,255,255), 2)
            ret, buffer = cv2.imencode('.jpg', blank)
            if ret:
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')
        time.sleep(0.03)  # ~30 FPS

@app.route('/video_feed')
def video_feed():
    return Response(generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/status')
def api_status():
    with state_lock:
        return jsonify(shared_state)

@app.route('/api/command', methods=['POST'])
def api_command():
    global awaiting_user_decision, is_full_auto_mode
    
    data = request.get_json()
    command = data.get("command", "").strip().upper()
    print(f"[🌐 WEB] Received command: {command}")

    if arduino and arduino.is_open:
        try:
            cmd_map = {
                "F_AUTO": b"F_AUTO\n",
                "F": b"F\n",
                "Y": b"Y\n",
                "N": b"N\n",
                "A": b"A\n",
                "X": b"X\n",
                "M": b"M\n"
            }
            
            if command == "M":
                is_full_auto_mode = not is_full_auto_mode
                log_event_to_sd("MODE_CHANGE", "FULL AUTO" if is_full_auto_mode else "SEMI-AUTO")
            
            if command == "F":
                awaiting_user_decision = True
                
            if command == "Y" and awaiting_user_decision:
                awaiting_user_decision = False
                log_event_to_sd("MANUAL_FIRE", "User approved via web")
                
            if command == "N" and awaiting_user_decision:
                awaiting_user_decision = False
                log_event_to_sd("MANUAL_CANCEL", "User denied via web")
                
            if command in cmd_map:
                arduino.write(cmd_map[command])
                log_event_to_sd("WEB_CMD", f"{command} (Remote)")
                return jsonify({"status": "sent", "command": command}), 200
            else:
                return jsonify({"error": "Unknown command"}), 400
        except Exception as e:
            return jsonify({"error": f"Failed to send: {e}"}), 500
    else:
        return jsonify({"error": "Arduino not connected"}), 503

# --- WEBSOCKET ---
@socketio.on('connect')
def handle_connect():
    print("[🌐] Client connected via WebSocket")
    with state_lock:
        # Create a copy without the frame
        state_copy = shared_state.copy()
        if 'frame' in state_copy:
            del state_copy['frame']  # Remove the frame which is a numpy array
        emit('status', state_copy)

@socketio.on('disconnect')
def handle_disconnect():
    print("[🌐] Client disconnected")

def update_websocket_state():
    while True:
        time.sleep(0.5)
        with state_lock:
            # Create a copy of shared_state without the frame (which is not JSON serializable)
            state_copy = shared_state.copy()
            if 'frame' in state_copy:
                del state_copy['frame']  # Remove the frame which is a numpy array
            socketio.emit('status', state_copy)

# --- CLEAR AUTHORIZATIONS ---
def clear_all_authorizations():
    if os.path.exists(AUTHORIZED_DB_PATH):
        shutil.rmtree(AUTHORIZED_DB_PATH)
        os.makedirs(AUTHORIZED_DB_PATH, exist_ok=True)
    if os.path.exists("dataset"):
        for f in os.listdir("dataset"):
            if f.endswith(".jpg"):
                os.remove(os.path.join("dataset", f))
    print("[✅] System reset. Ready to enroll new users.")

# --- ARDUINO SETUP ---
def connect_arduino():
    global arduino
    try:
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
        time.sleep(2)
        print(f"[✅] Connected to Arduino on {SERIAL_PORT}")
        arduino = ser
        return ser
    except Exception as e:
        print(f"[❌] Serial connection failed: {e}")
        arduino = None
        return None

# --- CAMERA INITIALIZER ---
def initialize_camera():
    global cap
    for idx in range(10):
        try:
            camera = cv2.VideoCapture(idx)
            if camera.isOpened():
                ret, _ = camera.read()
                if ret:
                    camera.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                    camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
                    camera.set(cv2.CAP_PROP_FPS, 30)
                    print(f"[✅] Camera initialized on index {idx}")
                    cap = camera
                    return camera
            camera.release()
        except Exception as e:
            print(f"[⚠️] Error with camera {idx}: {e}")
    print("[❌] No working camera found.")
    return None

# --- ARDUINO COMMUNICATION HELPERS ---
def send_arduino_cmd(cmd: bytes):
    if arduino and arduino.is_open:
        try:
            arduino.write(cmd)
            arduino.flush()  # Ensure data is sent immediately
            time.sleep(0.01)  # Give Arduino time to react
        except Exception as e:
            print(f"[⚠️] Serial write failed: {e}")

def get_arduino_response(timeout=0.1):
    if arduino and arduino.is_open:
        time.sleep(timeout)
        if arduino.in_waiting:
            raw = arduino.readline()
            try:
                return raw.decode('utf-8').strip()
            except UnicodeDecodeError:
                print(f"[⚠️ SERIAL] Ignoring invalid UTF-8 data: {raw}")
                return ""  # Return empty string on decode error
    return ""

def get_distances_from_arduino():
    send_arduino_cmd(b"GET_DIST\n")
    line = get_arduino_response()
    if line.startswith("DIST:"):
        try:
            parts = line.split(',')
            if len(parts) == 4:
                return [max(0, min(float(parts[i]), 500)) for i in range(1, 4)]
        except:
            pass
    return [0, 0, 0]

def get_temp_from_arduino():
    send_arduino_cmd(b"GET_TEMP\n")
    resp = get_arduino_response()
    try:
        return float(resp)
    except:
        return 0.0

def get_battery_from_arduino():
    send_arduino_cmd(b"GET_BATT\n")
    resp = get_arduino_response()
    try:
        return float(resp)
    except:
        return 0.0

def log_event_to_sd(event_type, details=""):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"{timestamp} | {event_type} | {details}"
    send_arduino_cmd(f"LOG:{log_entry}\n".encode('utf-8'))
    print(f"[📝 SD LOG] {log_entry}")
    
def send_telegram_message(message):
    """Send a message directly to Telegram from Python"""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        params = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }
        response = requests.get(url, params=params, timeout=5)
        if response.status_code == 200 and response.json().get("ok"):
            print(f"[📱] Telegram message sent successfully: {message}")
            return True
        else:
            print(f"[⚠️] Telegram API error: {response.text}")
            return False
    except Exception as e:
        print(f"[❌] Failed to send Telegram message: {e}")
        return False

def send_telegram_photo(image, caption=""):
    """Send a photo with optional caption to Telegram"""
    try:
        # Convert OpenCV image to JPEG bytes
        _, img_encoded = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 85])
        img_bytes = img_encoded.tobytes()
        
        # Prepare multipart/form-data
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
        files = {
            'photo': ('unauthorized_face.jpg', img_bytes, 'image/jpeg')
        }
        data = {
            'chat_id': TELEGRAM_CHAT_ID,
            'caption': caption,
            'parse_mode': 'HTML'
        }
        
        # Send the request
        response = requests.post(url, files=files, data=data, timeout=10)
        
        if response.status_code == 200 and response.json().get("ok"):
            print(f"[📷] Telegram photo sent successfully with caption: {caption}")
            return True
        else:
            print(f"[⚠️] Telegram photo API error: {response.text}")
            return False
    except Exception as e:
        print(f"[❌] Failed to send Telegram photo: {e}")
        return False

# --- FRAME CAPTURE THREAD ---
def capture_frames():
    global cap, enrollment_mode, enrollment_count, enrollment_name, awaiting_user_decision, is_full_auto_mode
    
    # Initialize camera
    if not cap:
        cap = initialize_camera()
    
    if cap is None:
        print("[❌] Failed to initialize camera. Exiting...")
        return
    
    # Initialize sensor throttling variables
    last_sensor_read = 0
    SENSOR_READ_INTERVAL = 0.2  # 5 Hz - read sensors every 200ms
    
    print("\n" + "="*80)
    print(" 🎯 NERF/GEL BLASTER SENTRY TURRET — FINAL VERSION")
    print("="*80)
    
    while True:
        try:
            ret, frame = cap.read()
            if not ret or frame is None:
                print("[⚠️] Lost camera feed. Attempting to reinitialize...")
                cap.release()
                time.sleep(1)
                cap = initialize_camera()
                if cap is None:
                    print("[❌] Camera unrecoverable. Exiting.")
                    break
                continue
            
            # Initialize tracking lists and flags
            targets = []
            authorized_faces = []
            unauthorized_faces = []
            human_bodies = []
            frame_height, frame_width = frame.shape[:2]
            
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            
            # Initialize faces_for_enroll for both modes
            faces_for_enroll = []
            
            # --- ENROLLMENT MODE ---
            if enrollment_mode:
                # Use YOLOv8 for face enrollment
                faces_for_enroll = []
                if face_model is not None:
                    try:
                        face_results = face_model(frame, verbose=False)
                        for result in face_results[0].boxes:
                            conf = float(result.conf[0])
                            if conf > 0.7:  # Higher confidence for enrollment
                                x1, y1, x2, y2 = map(int, result.xyxy[0])
                                faces_for_enroll.append((x1, y1, x2-x1, y2-y1))
                    except Exception as e:
                        print(f"[⚠️] Face enrollment detection error: {e}")
                if enrollment_count < ENROLLMENT_TARGET:
                    instruction = ENROLLMENT_STAGES[enrollment_count]
                    cv2.putText(frame, f"INSTRUCTION: {instruction}", (10, 180), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 165, 255), 2)
                    if arduino:
                        send_arduino_cmd(b"BEEP\n")
                
                cv2.putText(frame, f"ENROLLING: {enrollment_name} ({enrollment_count}/{ENROLLMENT_TARGET})",
                          (10, 150), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 165, 255), 2)
                
                if len(faces_for_enroll) > 0:
                    (x, y, w, h) = faces_for_enroll[0]
                    if w > 80 and h > 80:
                        cv2.rectangle(frame, (x, y), (x+w, y+h), (255, 200, 0), 3)
                        cv2.putText(frame, "✅ READY — Press 'C' to Capture", (x, y-15), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 200, 0), 2)
                    else:
                        cv2.putText(frame, "⚠️ Get closer for better capture", (10, 210), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 165, 255), 2)
                else:
                    cv2.putText(frame, "🛑 NO FACE — Please look at camera", (10, 210), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
                
                # Update frame but DO NOT continue - let the display and key handling happen
                with state_lock:
                    shared_state["frame"] = frame.copy()
                
                # Skip DeepFace processing during enrollment to avoid slowdowns
                skip_face_recognition = True
            else:
                skip_face_recognition = False
            
            # --- FACE DETECTION + DEEPFACE ---
            # Only run if not in enrollment mode
            faces = []
            if not skip_face_recognition and face_model is not None:
                # Use YOLOv8 for face detection
                try:
                    face_results = face_model(frame, verbose=False)
                    for result in face_results[0].boxes:
                        conf = float(result.conf[0])
                        if conf > 0.6:  # Confidence threshold
                            x1, y1, x2, y2 = map(int, result.xyxy[0])
                            x, y, w, h = x1, y1, x2-x1, y2-y1
                            if w < 60 or h < 60:
                                continue
                            faces.append((x, y, w, h))
                except Exception as e:
                    print(f"[⚠️] Face detection error: {e}")
                    
            # Process each face independently
            for (x, y, w, h) in faces:
                if w < 60 or h < 60:
                    continue
                
                # Initialize per-face variables
                authorized_for_this_face = False
                name_for_face = "Unknown"
                distance_metric = 1.0
                
                # Create a slightly smaller crop to avoid overlapping with nearby faces
                pad = 3  # Small padding to avoid edge contamination
                crop_x = max(0, x + pad)
                crop_y = max(0, y + pad)
                crop_w = min(w - 2*pad, frame_width - crop_x - 1)
                crop_h = min(h - 2*pad, frame_height - crop_y - 1)
                
                if crop_w <= 0 or crop_h <= 0:
                    continue  # Skip if crop dimensions are invalid
                
                face_color = frame[crop_y:crop_y+crop_h, crop_x:crop_x+crop_w]
                face_id = f"{x}_{y}_{w}_{h}"
                
                # Throttle DeepFace
                if time.time() - last_recognition_time.get(face_id, 0) > DEEPFACE_COOLDOWN:
                    try:
                        dfs = DeepFace.find(img_path=face_color, db_path=AUTHORIZED_DB_PATH,
                                            model_name="ArcFace", detector_backend="opencv",
                                            distance_metric="cosine", enforce_detection=False, silent=True)
                        deepface_threshold = 0.6  # Lower threshold for higher accuracy
                        
                        if len(dfs) > 0 and len(dfs[0]) > 0:
                            best_match = dfs[0].iloc[0]
                            distance_metric = best_match['distance']
                            matched_name = os.path.basename(best_match['identity']).split('_')[0]
                            if matched_name != "Unknown" and distance_metric < deepface_threshold:
                                authorized_for_this_face = True
                                name_for_face = matched_name
                        
                        last_recognition_time[face_id] = time.time()
                    except Exception as e:
                        print(f"[⚠️] DeepFace error: {e}")
                
                # Append to targets with individual authorization flag
                targets.append({
                    'type': 'face',
                    'name': name_for_face,
                    'authorized': authorized_for_this_face,
                    'distance_metric': distance_metric,
                    'bbox': (x, y, w, h),
                    'center': (x + w//2, y + h//2)
                })
            
            # --- HUMAN DETECTION ---
            if person_model is not None:
                try:
                    # Use YOLOv8 for person detection
                    person_results = person_model(frame, verbose=False)
                    for result in person_results[0].boxes:
                        cls = int(result.cls[0])
                        conf = float(result.conf[0])
                        if cls == 0 and conf > 0.5:  # class 0 = 'person'
                            x1, y1, x2, y2 = map(int, result.xyxy[0])
                            startX, startY, endX, endY = x1, y1, x2, y2
                            targets.append({'type': 'human', 'name': 'Human Body', 'confidence': conf,
                                           'bbox': (startX, startY, endX-startX, endY-startY),
                                           'center': ((startX+endX)//2, (startY+endY)//2)})
                except Exception as e:
                    print(f"[⚠️] Person detection error: {e}")
                    
            # --- AGGREGATE DETECTION FLAGS ---
            # Reset global flags and set them based on target properties
            authorized_detected = any(t.get('authorized', False) for t in targets if t['type'] == 'face')
            unauthorized_detected = any(not t.get('authorized', False) for t in targets if t['type'] == 'face')
            human_detected_from_body = any(t['type'] == 'human' for t in targets)
            
            # Send UNAUTHORIZED alert to Arduino if unauthorized face is detected
            if unauthorized_detected:
                if arduino and arduino.is_open:
                    try:
                        arduino.write(b"UNAUTHORIZED\n")
                        print("[PYTHON] Sent UNAUTHORIZED alert to Arduino")
                    except Exception as e:
                        print(f"[PYTHON] Failed to send UNAUTHORIZED alert: {e}")
                
                # --- 🔔 Send Telegram message with photo directly from Python ---
                try:
                    # Get information about the unauthorized faces
                    unauthorized_count = sum(1 for t in targets if t['type'] == 'face' and not t.get('authorized', False))
                    
                    # Create caption for the photo
                    caption = f"🚨 <b>NERF TURRET ALERT</b> 🚨\n\n"
                    caption += f"⚠️ <b>{unauthorized_count}</b> unauthorized face(s) detected!\n"
                    caption += f"🕒 Time: {datetime.now().strftime('%H:%M:%S')}\n"
                    caption += f"🔫 Mode: {'FULL AUTO' if is_full_auto_mode else 'SEMI-AUTO'}\n"
                    
                    # Find the first unauthorized face to send as photo
                    unauthorized_face = None
                    for t in targets:
                        if t['type'] == 'face' and not t.get('authorized', False):
                            x, y, w, h = t['bbox']
                            # Add some padding around the face
                            pad = 20
                            x1 = max(0, x - pad)
                            y1 = max(0, y - pad)
                            x2 = min(frame.shape[1], x + w + pad)
                            y2 = min(frame.shape[0], y + h + pad)
                            unauthorized_face = frame[y1:y2, x1:x2].copy()
                            break
                    
                    if unauthorized_face is not None and unauthorized_face.size > 0:
                        # Send the cropped face image with caption
                        send_telegram_photo(unauthorized_face, caption)
                        print("[PYTHON] Direct Telegram photo alert sent for unauthorized face")
                    else:
                        # Fallback to sending just the message if face crop failed
                        send_telegram_message(caption)
                        print("[PYTHON] Direct Telegram text alert sent (face crop failed)")
                except Exception as e:
                    print(f"[PYTHON] Failed to send direct Telegram alert: {e}")
            
            # --- TARGET PRIORITIZATION & AIMING ---
            primary_target = None
            laser_on = False
            if targets:
                # Get current distance values (already throttled above)
                with state_lock:
                    current_distances = [shared_state["distance_front"], 
                                        shared_state["distance_left"], 
                                        shared_state["distance_right"]]
                
                for target in targets:
                    tx, _ = target['center']
                    if tx < frame_width // 3:
                        est_dist = current_distances[1]  # Left sensor
                    elif tx > 2 * frame_width // 3:
                        est_dist = current_distances[2]  # Right sensor
                    else:
                        est_dist = current_distances[0]  # Front sensor
                    est_dist = est_dist if 0 < est_dist <= 500 else 200
                    target['estimated_distance'] = est_dist
                    target['priority'] = est_dist
                
                targets.sort(key=lambda t: t['priority'])
                primary_target = targets[0]
                
                # Calculate angles
                pan_angle = int(np.interp(primary_target['center'][0], [0, frame_width], [180, 0]))
                tilt_angle = int(np.interp(primary_target['center'][1], [0, frame_height], [180, 0]))
                pan_angle = max(30, min(150, pan_angle))
                tilt_angle = max(40, min(140, tilt_angle))
                
                # Send commands
                send_arduino_cmd(f"PAN:{pan_angle}\n".encode())
                time.sleep(0.02)
                send_arduino_cmd(f"TILT:{tilt_angle}\n".encode())
                time.sleep(0.02)
                send_arduino_cmd(b"LASER_ON\n")
                laser_on = True
                
                # Draw crosshair
                tx, ty = primary_target['center']
                cv2.drawMarker(frame, (tx, ty), (255, 0, 0), markerType=cv2.MARKER_CROSS, markerSize=20, thickness=2)
                
                # Auto-fire logic - use per-target authorization flag
                if is_full_auto_mode and (
                    (primary_target['type'] == 'face' and not primary_target.get('authorized', False)) 
                    or primary_target['type'] == 'human'
                ):
                    print("\n[🚨 AUTO-FIRE TRIGGERED!]")
                    log_event_to_sd("AUTO_FIRE", f"Target: {primary_target['name']}, Distance: {primary_target['estimated_distance']:.1f}cm")
                    send_arduino_cmd(b"F_AUTO\n")
                
                # Semi-auto notification - use per-target authorization flag
                elif not is_full_auto_mode and (
                    (primary_target['type'] == 'face' and not primary_target.get('authorized', False)) 
                    or primary_target['type'] == 'human'
                ) and not awaiting_user_decision:
                    print("\n[🎯] TARGET DETECTED — AWAITING USER COMMAND VIA WEB")
                    send_arduino_cmd(b"F\n")  # Send alert to Arduino for ESP32 to forward to Telegram
                    awaiting_user_decision = True
            else:
                send_arduino_cmd(b"LASER_OFF\n")
                laser_on = False
            
            # --- DRAW COLOR-CODED BOUNDING BOXES FOR ALL TARGETS ---
            for t in targets:
                x, y, w, h = t['bbox']
                name = t['name']
                target_type = t['type']

                # Color logic based on per-target authorization status
                if target_type == 'face':
                    if t.get('authorized', False):
                        color = (0, 255, 0)  # Green - authorized
                    else:
                        color = (0, 0, 255)  # Red - unauthorized
                elif target_type == 'human':
                    color = (0, 165, 255)    # Orange - human body
                else:
                    color = (255, 255, 0)    # Yellow - other types

                # Draw bounding box with thicker lines
                cv2.rectangle(frame, (x, y), (x+w, y+h), color, 3)

                # Show name above the box with confidence/distance metric
                if target_type == 'face':
                    auth_status = "AUTH" if t.get('authorized', False) else "UNAUTH"
                    display_name = f"{name} ({t['distance_metric']:.2f}) {auth_status}"
                elif target_type == 'human':
                    display_name = f"HUMAN ({t['confidence']:.2f})"
                else:
                    display_name = target_type.upper()
                    
                cv2.putText(frame, display_name, (x, y-10),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)

            # --- UPDATE SHARED STATE ---
            # Throttle sensor reads to prevent flooding the serial line
            current_time = time.time()
            if current_time - last_sensor_read > SENSOR_READ_INTERVAL:
                distances = get_distances_from_arduino()
                tempC = get_temp_from_arduino()
                batteryVoltage = get_battery_from_arduino()
                last_sensor_read = current_time
            else:
                # Reuse previous values from shared_state
                with state_lock:
                    distances = [shared_state["distance_front"], shared_state["distance_left"], shared_state["distance_right"]]
                    tempC = shared_state["temperature"]
                    batteryVoltage = shared_state["battery"]
            
            status_text = "AUTHORIZED PRESENT" if authorized_detected else \
                          "UNAUTHORIZED DETECTED" if unauthorized_detected else \
                          "HUMAN (NO FACE)" if human_detected_from_body else "CLEAR"
            mode_text = "FULL AUTO" if is_full_auto_mode else "SEMI-AUTO"
            
            with state_lock:
                shared_state.update({
                    "frame": frame.copy(),
                    "status": status_text,
                    "mode": mode_text,
                    "faces": len(faces),
                    "distance_front": distances[0],
                    "distance_left": distances[1],
                    "distance_right": distances[2],
                    "temperature": round(tempC, 1),
                    "battery": round(batteryVoltage, 1),
                    "laser_on": laser_on,
                    "authorized_detected": authorized_detected,
                    "unauthorized_detected": unauthorized_detected,
                    "human_detected": human_detected_from_body,
                    "awaiting_decision": awaiting_user_decision,
                    "target_center": {"x": int(primary_target['center'][0]) if primary_target else 0,
                                      "y": int(primary_target['center'][1]) if primary_target else 0},
                    "targets": [{"type": t["type"], "name": t["name"],
                                 "dist": round(t["estimated_distance"], 1) if "estimated_distance" in t else 0}
                                for t in targets],
                    "last_update": datetime.now().strftime("%H:%M:%S")
                })
            
            # Display overlays
            cv2.putText(frame, f"Faces: {len(faces)}", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 0), 2)
            cv2.putText(frame, f"Status: {status_text}", (10, 90), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255) if "UNAUTHORIZED" in status_text else (0, 255, 0), 2)
            cv2.putText(frame, f"Mode: {mode_text}", (10, 120), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0) if is_full_auto_mode else (255, 255, 0), 2)
            cv2.putText(frame, "Web Dashboard: http://10.29.49.14:5000", (10, 430), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
            cv2.putText(frame, "KEYS: A=Enroll, C=Capture, X=Clear All, M=Toggle Mode, SPACE=Request Fire, Q=Quit",
                      (10, 470), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
            
            # --- LOCAL DISPLAY FOR ENROLLMENT & DEBUGGING ---
            cv2.imshow("Nerf Sentry Turret — Face or Human Detection", frame)
            key = cv2.waitKey(1) & 0xFF

            # Handle keyboard commands
            if key == ord('a') or key == ord('A'):
                if not enrollment_mode:
                    # Pause camera display to get input
                    cv2.putText(frame, "Enter name in console and press Enter...", (10, 240), 
                              cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 165, 255), 2)
                    cv2.imshow("Nerf Sentry Turret — Face or Human Detection", frame)
                    cv2.waitKey(1)
                    
                    # Get custom name from user
                    try:
                        print("\n[👤] Enter name for face enrollment: ", end='', flush=True)
                        custom_name = input().strip()
                        
                        # If empty, use timestamp as fallback
                        if not custom_name:
                            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                            custom_name = f"User_{timestamp}"
                            print(f"[ℹ️] Using default name: {custom_name}")
                        
                        enrollment_mode = True
                        enrollment_name = custom_name
                        enrollment_count = 0
                        print(f"[✅] ENROLLMENT STARTED for '{enrollment_name}'")
                        print("[📸] Follow on-screen pose instructions. Press 'C' to capture each pose.")
                    except Exception as e:
                        print(f"[❌] Error during name input: {e}")
                        print("[ℹ️] Using default name instead")
                        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                        enrollment_name = f"User_{timestamp}"
                        enrollment_mode = True
                        enrollment_count = 0
                else:
                    print("[⚠️] Enrollment already in progress. Finish or restart.")

            elif key == ord('c') or key == ord('C'):
                if enrollment_mode and enrollment_count < ENROLLMENT_TARGET:
                    # Detect faces for enrollment using YOLOv8
                    faces_enroll = []
                    if face_model is not None:
                        try:
                            face_results = face_model(frame, verbose=False)
                            for result in face_results[0].boxes:
                                conf = float(result.conf[0])
                                if conf > 0.7:  # Higher confidence for enrollment
                                    x1, y1, x2, y2 = map(int, result.xyxy[0])
                                    faces_enroll.append((x1, y1, x2-x1, y2-y1))
                        except Exception as e:
                            print(f"[⚠️] Face capture detection error: {e}")
                    
                    if len(faces_enroll) > 0:
                        # Select the LARGEST face (by area)
                        largest_face = max(faces_enroll, key=lambda f: f[2] * f[3])
                        (x, y, w, h) = largest_face
                        
                        if w >= 80 and h >= 80:
                            face_img = frame[y:y+h, x:x+w]
                            timestamp_img = datetime.now().strftime("%Y%m%d_%H%M%S")
                            filename = f"{AUTHORIZED_DB_PATH}/{enrollment_name}_{timestamp_img}_{enrollment_count}.jpg"
                            
                            try:
                                cv2.imwrite(filename, face_img)
                                print(f"[✅] Captured pose {enrollment_count + 1}/{ENROLLMENT_TARGET}")
                                enrollment_count += 1
                                
                                if enrollment_count >= ENROLLMENT_TARGET:
                                    print(f"\n[🎉] ENROLLMENT COMPLETE for '{enrollment_name}'!")
                                    enrollment_mode = False
                                    enrollment_name = ""
                                    enrollment_count = 0
                                    # Optional: Refresh DeepFace cache
                                    shutil.rmtree("deepface_cache", ignore_errors=True)
                            except Exception as e:
                                print(f"[❌] Failed to save image: {e}")
                        else:
                            print("[⚠️] Face too small. Get closer.")
                    else:
                        print("[⚠️] No face detected. Look at the camera.")

            elif key == ord('x') or key == ord('X'):
                clear_all_authorizations()
                enrollment_mode = False
                enrollment_name = ""
                enrollment_count = 0
                print("\n[🗑️] All authorized faces cleared!")

            elif key == ord('m') or key == ord('M'):
                is_full_auto_mode = not is_full_auto_mode
                mode_str = "FULL AUTO" if is_full_auto_mode else "SEMI-AUTO"
                print(f"\n[🔄] Mode switched to: {mode_str}")

            elif key == ord(' '):  # SPACE key
                if arduino and not is_full_auto_mode:
                    send_arduino_cmd(b"F\n")
                    awaiting_user_decision = True
                    print("\n[🔫] Fire requested — awaiting confirmation")
            elif key == ord('q') or key == ord('Q'):
                print("🛑 Quitting...")
                break
            
            time.sleep(0.03)  # ~30 FPS
            
        except Exception as e:
            print(f"[⚠️] Frame processing error: {e}")
            time.sleep(1)

# --- MAIN EXECUTION ---
if __name__ == '__main__':
    # Connect to Arduino
    connect_arduino()
    
    # Initialize camera
    cap = initialize_camera()
    if cap is None:
        print("🛑 Camera required. Exiting.")
        sys.exit(1)
    
    # Send startup message to Telegram directly from Python
    startup_message = "🤖 <b>NERF TURRET SYSTEM STARTING</b>\n\n"
    startup_message += "✅ Python detection system online\n"
    startup_message += "✅ Direct Telegram messaging enabled\n"
    startup_message += "✅ Photo alerts enabled\n"
    startup_message += f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    send_telegram_message(startup_message)
    
    # Create a test image to verify photo sending works
    try:
        # Create a simple test image with text
        test_img = np.zeros((300, 400, 3), dtype=np.uint8)
        cv2.putText(test_img, "NERF TURRET SYSTEM", (30, 80), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 165, 255), 2)
        cv2.putText(test_img, "CAMERA TEST", (100, 130), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 165, 255), 2)
        cv2.putText(test_img, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), (70, 200), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
        
        # Send test image
        send_telegram_photo(test_img, "📷 Camera system initialized and ready")
        print("[PYTHON] Test photo sent to Telegram")
    except Exception as e:
        print(f"[PYTHON] Failed to send test photo: {e}")
    
    # 👇 START FLASK SERVER IN BACKGROUND THREAD
    flask_thread = threading.Thread(target=start_flask_server, daemon=True)
    flask_thread.start()
    time.sleep(1)  # Let Flask start
    
    # Start websocket update thread
    threading.Thread(target=update_websocket_state, daemon=True).start()
    
    # Start frame capture in main thread
    capture_frames()
    
    # Cleanup
    print("[🛑] Shutting down...")
    if cap:
        cap.release()
    cv2.destroyAllWindows()

    