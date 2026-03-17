<p align="center">
  <img src="assets/visionguard51-banner.svg" alt="VisionGuard51 banner" width="100%" />
</p>

<p align="center">
  <b>VisionGuard51: AI-Powered Neural Sentinel Turret</b><br/>
  Real-time perception, authorization, remote ops, and alerting — stitched into a single Python control loop.
</p>

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.10%2B-0B1024?style=flat&labelColor=0B1024&color=27E0C6" />
  <img alt="OpenCV" src="https://img.shields.io/badge/OpenCV-Computer%20Vision-0B1024?style=flat&labelColor=0B1024&color=FFB84D" />
  <img alt="YOLOv8" src="https://img.shields.io/badge/YOLOv8-Detection-0B1024?style=flat&labelColor=0B1024&color=27E0C6" />
  <img alt="Flask" src="https://img.shields.io/badge/Flask-Dashboard-0B1024?style=flat&labelColor=0B1024&color=FFB84D" />
</p>

<p align="center">
  <a href="#what-it-does">What it does</a> •
  <a href="#system-overview">System overview</a> •
  <a href="#quickstart-windows">Quickstart</a> •
  <a href="#models--weights">Models</a> •
  <a href="#dashboard">Dashboard</a> •
  <a href="#safety--ethics">Safety</a>
</p>

## What it does

- **Detects** people + faces (YOLOv8), then **classifies** faces as authorized/unauthorized (DeepFace / ArcFace).
- **Tracks targets** and exposes a **live dashboard** (Flask + Socket.IO) with video, telemetry, and commands.
- **Controls hardware** over serial (Arduino) for actuation + sensor gates.
- **Sends alerts** via Telegram (text + photo evidence).

## System overview

<p align="center">
  <img src="assets/visionguard51-overview.svg" alt="VisionGuard51 system overview diagram" width="100%" />
</p>

## Tech stack

- **Core**: Python, OpenCV, NumPy  
- **Detection**: Ultralytics YOLOv8 (`yolov8n.pt`, `yolov8n-face.pt`)  
- **Face recognition**: DeepFace (ArcFace)  
- **Dashboard**: Flask + Flask-SocketIO (WebSocket status + MJPEG stream)  
- **Hardware**: Arduino via `pyserial`  
- **Alerts**: Telegram Bot API (`requests`)  

## Quickstart (Windows)

### 1) Create a virtual environment

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -U pip
```

### 2) Install dependencies

```powershell
pip install opencv-python numpy flask flask-socketio pyserial deepface ultralytics requests
```

### 3) Configure

Edit `basic_face_detection.py`:

- **Serial**: set `SERIAL_PORT` to your Arduino port (example: `COM7`)
- **Telegram**: set `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`

### 4) Run

```powershell
python basic_face_detection.py
```

## Models / weights

This repo **does not ship large weights** (GitHub blocks files >100MB). Download what you need locally.

### YOLOv8 weights

Place these next to the script (or adjust paths inside the code):

- `yolov8n.pt`
- `yolov8n-face.pt`

### OpenPose (MPI) model (if you use it)

- **Direct URL**: `http://vcl.snu.ac.kr/OpenPose/models/pose/mpi/pose_iter_160000.caffemodel`
- **Official reference script**: `https://raw.githubusercontent.com/CMU-Perceptual-Computing-Lab/openpose/master/models/getModels.sh`

Download to project root:

```powershell
Invoke-WebRequest -Uri "http://vcl.snu.ac.kr/OpenPose/models/pose/mpi/pose_iter_160000.caffemodel" -OutFile "pose_iter_160000.caffemodel"
```

## Dashboard

When the script starts, it launches a web UI:

- **Local**: `http://localhost:5000`
- **LAN**: `http://<your-ip>:5000`

Typical controls include:

- **Mode toggle** (semi-auto / full-auto)
- **Fire request** + approve/deny workflow (semi-auto)
- **Live feed** + current detections / targets

## Repo hygiene

- Large weight files are ignored by default (`*.caffemodel`, `*.pt`, etc.).
- Local face data folders like `authorized_db/` are ignored to protect privacy.

## Safety & ethics

This project is for **research/education**. If you attach actuation hardware:

- Use **physical interlocks**, a hard **E‑stop**, and conservative defaults.
- Never deploy where people can be harmed.
- Keep secrets out of git (move tokens to environment variables).

## License

Add a `LICENSE` file if you plan to distribute this project.
