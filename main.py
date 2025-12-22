import os
import shutil
import subprocess
import threading
import time
import sqlite3
import json
import tkinter as tk
from tkinter import filedialog
import psutil
import re
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from gpu_mon import GPUWatchdog

# --- CONFIGURATION ---
DEFAULT_CODEC_FALLBACK = "hevc_nvenc" 
OUTPUT_SUFFIX = "_compressed"
OUTPUT_EXT = ".mp4" 

app = FastAPI()
templates = Jinja2Templates(directory="templates")
watchdog = GPUWatchdog()

# Global paths
FFMPEG_BIN = "ffmpeg"
FFPROBE_BIN = "ffprobe"

# Global Process Tracker (for immediate kill)
ACTIVE_JOBS = {} 
AVAILABLE_CODECS = []

# --- HELPER: GET DURATION ---
def get_video_duration(input_path):
    """Returns video duration in seconds using ffprobe, falling back to ffmpeg"""
    global FFPROBE_BIN, FFMPEG_BIN
    
    # 1. Try FFprobe
    try:
        probe_exe = FFPROBE_BIN
        if not os.path.exists(probe_exe) and shutil.which("ffprobe"):
            probe_exe = "ffprobe"

        if os.path.exists(probe_exe) or shutil.which(probe_exe):
            cmd = [probe_exe, 
                   "-v", "error", "-show_entries", "format=duration", 
                   "-of", "default=noprint_wrappers=1:nokey=1", input_path]
            
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

            print(f"DEBUG: Running ffprobe on: {input_path}", flush=True)
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, 
                                    universal_newlines=True, startupinfo=startupinfo)
            
            val = result.stdout.strip()
            if val and val != "N/A":
                print(f"DEBUG: ffprobe duration: {val}", flush=True)
                return float(val)
    except Exception as e:
        print(f"DEBUG: ffprobe failed: {e}", flush=True)

    # 2. Fallback: Try ffmpeg (parse stderr)
    print("DEBUG: ffprobe failed or not found. Trying ffmpeg fallback...", flush=True)
    try:
        cmd = [FFMPEG_BIN, "-i", input_path]
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, 
                                universal_newlines=True, startupinfo=startupinfo)
        
        match = re.search(r"Duration: (\d{2}):(\d{2}):(\d{2}\.\d{2})", result.stderr)
        if match:
            hours, minutes, seconds = map(float, match.groups())
            total_seconds = hours * 3600 + minutes * 60 + seconds
            print(f"DEBUG: ffmpeg parsed duration: {total_seconds}", flush=True)
            return total_seconds
    except Exception as e:
        print(f"DEBUG: ffmpeg fallback failed: {e}", flush=True)

    return 0

# --- STARTUP CHECKS ---
def check_binaries():
    global FFMPEG_BIN, FFPROBE_BIN
    print("--- STARTUP CHECK ---", flush=True)
    
    local_ffmpeg = os.path.join(os.getcwd(), "ffmpeg.exe")
    if os.path.exists(local_ffmpeg):
        print(f"📍 Found local 'ffmpeg.exe'.", flush=True)
        FFMPEG_BIN = local_ffmpeg
    elif shutil.which("ffmpeg"):
        FFMPEG_BIN = "ffmpeg"
    else:
        print("❌ CRITICAL: 'ffmpeg' not found.", flush=True)

    local_ffprobe = os.path.join(os.getcwd(), "ffprobe.exe")
    if os.path.exists(local_ffprobe):
        print(f"📍 Found local 'ffprobe.exe'.", flush=True)
        FFPROBE_BIN = local_ffprobe
    elif shutil.which("ffprobe"):
        FFPROBE_BIN = "ffprobe"
    else:
        print("⚠️ WARNING: 'ffprobe' not found. Trying to locate...", flush=True)
        if "ffmpeg.exe" in FFMPEG_BIN:
            guess = FFMPEG_BIN.replace("ffmpeg.exe", "ffprobe.exe")
            if os.path.exists(guess):
                 FFPROBE_BIN = guess
                 print(f"   (Fixed) Found ffprobe at: {guess}", flush=True)

def detect_hardware_codecs():
    """Tests GPU support by running a tiny dummy encode"""
    global AVAILABLE_CODECS, FFMPEG_BIN
    print("--- DETECTING HARDWARE SUPPORT ---", flush=True)
    
    candidates = [
        ("av1_nvenc", "AV1 (NVENC)"),
        ("hevc_nvenc", "H.265/HEVC (NVENC)"),
        ("h264_nvenc", "H.264/AVC (NVENC)")
    ]
    
    verified = []
    
    for cid, cname in candidates:
        print(f"Testing {cid}...", end=" ", flush=True)
        try:
            # Attempt to encode 1 second of black video using standard settings
            # -pix_fmt yuv420p is critical for NVENC compliance test
            cmd = [
                FFMPEG_BIN, "-y", "-v", "error", 
                "-f", "lavfi", "-i", "color=c=black:s=1280x720:d=1",
                "-c:v", cid, "-pix_fmt", "yuv420p",
                "-f", "null", "-"
            ]
            
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, 
                                    universal_newlines=True, startupinfo=startupinfo)
            
            if result.returncode == 0:
                print("✅ Supported", flush=True)
                verified.append({"id": cid, "name": cname})
            else:
                print(f"❌ Failed. Error output:", flush=True)
                print(result.stderr.strip(), flush=True)
        except Exception as e:
            print(f"❌ Error: {e}", flush=True)
            
    if not verified:
        print("⚠️ No Hardware Codecs detected! Fallback to CPU.", flush=True)
        verified.append({"id": "libx265", "name": "H.265 (CPU - Slow)"})
        
    AVAILABLE_CODECS = verified
    print("----------------------------------", flush=True)

# Run checks on load
check_binaries()
detect_hardware_codecs()

# Database Setup
def init_db():
    conn = sqlite3.connect("queue.db", check_same_thread=False)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS queue 
                 (id INTEGER PRIMARY KEY, filename TEXT, status TEXT, progress INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS config 
                 (key TEXT PRIMARY KEY, value TEXT)''')
    
    # Pick best available codec as default
    best_codec = AVAILABLE_CODECS[0]["id"] if AVAILABLE_CODECS else DEFAULT_CODEC_FALLBACK

    defaults = {
        "cq": "24", 
        "preset": "p7", 
        "resolution": "Original",
        "codec": best_codec
    }
    
    for k, v in defaults.items():
        c.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", (k, v))

    # Migration 1: Force update preset to p7 if it was the old default p6
    c.execute("UPDATE config SET value='p7' WHERE key='preset' AND value='p6'")

    # Migration 2: Validate Stored Codec against Hardware
    # If the DB says 'av1' but hardware says 'hevc', force update the DB
    c.execute("SELECT value FROM config WHERE key='codec'")
    row = c.fetchone()
    if row:
        stored_codec = row[0]
        valid_ids = [x['id'] for x in AVAILABLE_CODECS]
        if valid_ids and stored_codec not in valid_ids:
            print(f"DEBUG: Stored codec '{stored_codec}' not supported by current hardware.", flush=True)
            print(f"DEBUG: Resetting codec to '{valid_ids[0]}'", flush=True)
            c.execute("UPDATE config SET value=? WHERE key='codec'", (valid_ids[0],))

    # Reset unfinished jobs on startup
    c.execute("UPDATE queue SET status='PENDING', progress=0 WHERE status='ENCODING' OR status='MANUAL_PAUSE'")
    conn.commit()
    conn.close()

init_db()

def get_db():
    return sqlite3.connect("queue.db", check_same_thread=False)

def get_config_value(key):
    db = get_db()
    row = db.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    db.close()
    return row[0] if row else None

def set_config_value(key, value):
    db = get_db()
    db.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, value))
    db.commit()
    db.close()

def update_job_status(job_id, status, progress=None):
    db = get_db()
    if progress is not None:
        db.execute("UPDATE queue SET status=?, progress=? WHERE id=?", (status, progress, job_id))
    else:
        db.execute("UPDATE queue SET status=? WHERE id=?", (status, job_id))
    db.commit()
    db.close()

# --- WORKER LOOP ---
def worker():
    global ACTIVE_JOBS
    while True:
        watch_dir = get_config_value("watch_dir")
        if not watch_dir or not os.path.exists(watch_dir):
            time.sleep(2)
            continue

        db = get_db()
        job = db.execute("SELECT id, filename FROM queue WHERE status='PENDING' LIMIT 1").fetchone()
        db.close()

        if not job:
            time.sleep(2)
            continue

        job_id, filename = job
        input_path = os.path.join(watch_dir, filename)
        output_filename = os.path.splitext(filename)[0] + OUTPUT_SUFFIX + OUTPUT_EXT
        output_path = os.path.join(watch_dir, output_filename)

        print(f"DEBUG: Calculating Duration for {filename}", flush=True)
        total_duration = get_video_duration(input_path)
        print(f"DEBUG: Duration found: {total_duration}s", flush=True)

        # Settings
        cq = get_config_value("cq") or "24"
        preset = get_config_value("preset") or "p7"
        res = get_config_value("resolution") or "Original"
        
        # Get stored codec but VALIDATE against hardware availability
        stored_codec = get_config_value("codec")
        valid_codec_ids = [c["id"] for c in AVAILABLE_CODECS]
        
        if stored_codec and stored_codec in valid_codec_ids:
            codec = stored_codec
        elif valid_codec_ids:
             # If DB has garbage or unsupported codec, fallback safely
            codec = valid_codec_ids[0]
        else:
            codec = DEFAULT_CODEC_FALLBACK

        update_job_status(job_id, "ENCODING", 0)

        cmd = [
            FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error",
            "-progress", "pipe:1",
            "-i", input_path,
            "-c:v", codec, 
            "-preset", preset,
        ]

        # Apply correct quality flags based on codec type
        if "nvenc" in codec:
            # NVENC uses -cq with -rc vbr for "Constant Quality" mode
            # -b:v 0 uncaps the bitrate to allow quality to dictate it
            cmd.extend(["-rc", "vbr", "-cq", cq, "-b:v", "0"])
        else:
            # CPU (libx265/x264) uses -crf for Constant Rate Factor
            cmd.extend(["-crf", cq])

        if res == "1080p": cmd.extend(["-vf", "scale=-1:1080"])
        elif res == "720p": cmd.extend(["-vf", "scale=-1:720"])
        
        cmd.extend(["-c:a", "copy", output_path])

        print(f"DEBUG: Starting Job #{job_id} using {codec}", flush=True)

        try:
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, 
                universal_newlines=True, startupinfo=startupinfo
            )
            
            # REGISTER PROCESS GLOBALLY
            ACTIVE_JOBS[job_id] = proc
            proc_obj = psutil.Process(proc.pid)
            
            # Init tracker
            current_status_row = None 
            
            while proc.poll() is None:
                # 1. Check DB for Manual Signals
                db = get_db()
                current_status_row = db.execute("SELECT status FROM queue WHERE id=?", (job_id,)).fetchone()
                db.close()

                if not current_status_row:
                    print("DEBUG: Job deleted from DB. Killing process.", flush=True)
                    proc.kill()
                    break
                
                status = current_status_row[0]

                if status == "MANUAL_PAUSE":
                    try:
                        if proc_obj.status() != psutil.STATUS_STOPPED:
                            proc_obj.suspend()
                    except: pass
                    time.sleep(1)
                    continue 

                elif status == "ENCODING":
                    try:
                        if proc_obj.status() == psutil.STATUS_STOPPED:
                            proc_obj.resume()
                    except: pass
                    
                    watchdog_state = watchdog.manage_process(proc.pid)
                    
                    if watchdog_state == "RUNNING":
                        line = proc.stdout.readline()
                        if not line: break
                        
                        if "out_time_us=" in line:
                            try:
                                val = line.split("=")[1].strip()
                                if val and val != "N/A":
                                    us = int(val)
                                    if total_duration > 0:
                                        percent = int((us / 1000000 / total_duration) * 100)
                                        update_job_status(job_id, "ENCODING", percent)
                            except: pass

                else:
                    time.sleep(1)

            # FIX: Force wait for the return code to populate to prevent false "Failed" errors
            proc.wait()

            # UNREGISTER
            if job_id in ACTIVE_JOBS:
                del ACTIVE_JOBS[job_id]

            if proc.returncode == 0:
                print(f"DEBUG: Job #{job_id} Completed.", flush=True)
                update_job_status(job_id, "COMPLETED", 100)
            elif proc.returncode != 0 and current_status_row: 
                print(f"DEBUG: Job #{job_id} FAILED/CANCELLED. Output:\n{proc.stdout.read()}", flush=True)
                update_job_status(job_id, "FAILED", 0)

        except Exception as e:
            print(f"CRITICAL ERROR: {e}", flush=True)
            update_job_status(job_id, "ERROR", 0)
            if job_id in ACTIVE_JOBS:
                del ACTIVE_JOBS[job_id]

# Start Worker
t = threading.Thread(target=worker, daemon=True)
t.start()

# --- ROUTES ---

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    watch_dir = get_config_value("watch_dir")
    
    settings = {
        "cq": get_config_value("cq") or "24",
        "preset": get_config_value("preset") or "p7",
        "resolution": get_config_value("resolution") or "Original",
        "codec": get_config_value("codec") or (AVAILABLE_CODECS[0]["id"] if AVAILABLE_CODECS else DEFAULT_CODEC_FALLBACK)
    }

    files = []
    error = None
    if watch_dir and os.path.exists(watch_dir):
        try:
            files = sorted([f for f in os.listdir(watch_dir) 
                           if f.lower().endswith(('.mkv', '.mp4', '.avi', '.mov', '.webm'))])
        except Exception as e: error = str(e)
    
    db = get_db()
    queue = db.execute("SELECT * FROM queue").fetchall()
    db.close()
    
    return templates.TemplateResponse("index.html", {
        "request": request, 
        "files": files, 
        "queue": queue, 
        "watch_dir": watch_dir, 
        "error": error, 
        "settings": settings,
        "codecs": AVAILABLE_CODECS
    })

@app.get("/files_list", response_class=HTMLResponse)
async def files_list(request: Request):
    """Returns just the HTML list of files for the refresh button"""
    watch_dir = get_config_value("watch_dir")
    files = []
    
    if watch_dir and os.path.exists(watch_dir):
        try:
            files = sorted([f for f in os.listdir(watch_dir) 
                           if f.lower().endswith(('.mkv', '.mp4', '.avi', '.mov', '.webm'))])
        except: pass
    
    if not files:
        return '<div class="text-gray-500 text-center mt-10">No files found.</div>'

    html = ""
    for file in files:
        html += f"""
        <form hx-post="/add" class="flex justify-between items-center bg-gray-700 p-2 mb-2 rounded hover:bg-gray-600 transition">
            <span class="truncate w-2/3 text-sm" title="{file}">{file}</span>
            <input type="hidden" name="filename" value="{file}">
            <button type="submit" class="bg-blue-600 hover:bg-blue-500 px-3 py-1 rounded text-xs font-bold">Queue</button>
        </form>
        """
    return html

@app.post("/set_path")
async def set_path(path: str = Form(...)):
    set_config_value("watch_dir", path.strip())
    return RedirectResponse(url="/", status_code=303)

@app.post("/save_settings")
async def save_settings(cq: str = Form(...), preset: str = Form(...), resolution: str = Form(...), codec: str = Form(...)):
    set_config_value("cq", cq)
    set_config_value("preset", preset)
    set_config_value("resolution", resolution)
    set_config_value("codec", codec)
    return HTMLResponse(content="<span class='text-green-400 text-xs ml-2'>Saved!</span>")

@app.post("/browse")
async def browse_folder():
    try:
        root = tk.Tk(); root.withdraw(); root.attributes('-topmost', True)
        folder = filedialog.askdirectory(); root.destroy()
        if folder: return HTMLResponse(f"""<input type="text" name="path" value="{os.path.normpath(folder)}" class="bg-gray-800 text-sm p-2 rounded w-full border border-gray-600">""")
    except: pass
    return HTMLResponse(f"""<input type="text" name="path" placeholder="Error" class="bg-gray-800 text-sm p-2 rounded w-full">""")

@app.post("/add")
async def add_job(filename: str = Form(...)):
    db = get_db()
    exists = db.execute("SELECT id FROM queue WHERE filename=? AND status!='COMPLETED'", (filename,)).fetchone()
    if not exists:
        db.execute("INSERT INTO queue (filename, status, progress) VALUES (?, 'PENDING', 0)", (filename,))
        db.commit()
    db.close()
    return HTMLResponse(content="", headers={"HX-Refresh": "true"})

@app.post("/control/{job_id}/{action}")
async def job_control(job_id: int, action: str):
    print(f"Control Request: {action} Job #{job_id}", flush=True)
    db = get_db()
    
    if action == "delete":
        # IMMEDIATE KILL if active
        if job_id in ACTIVE_JOBS:
            print(f"DEBUG: Immediate Kill requested for Job #{job_id}", flush=True)
            try:
                ACTIVE_JOBS[job_id].kill()
            except: pass
        
        db.execute("DELETE FROM queue WHERE id=?", (job_id,))

    elif action == "pause":
        db.execute("UPDATE queue SET status='MANUAL_PAUSE' WHERE id=?", (job_id,))
    elif action == "resume":
        db.execute("UPDATE queue SET status='PENDING' WHERE status='MANUAL_PAUSE' AND id=?", (job_id,))
        db.execute("UPDATE queue SET status='ENCODING' WHERE id=?", (job_id,))
        
    db.commit()
    db.close()
    return HTMLResponse(content="", headers={"HX-Refresh": "true"})

@app.get("/status_bar")
async def status_bar():
    load = watchdog.get_3d_load()
    color = "text-green-500" if load < 75 else "text-red-500"
    status_text = "Idle (Encoding)" if load < 75 else "Gaming (Paused)"
    return HTMLResponse(f"""<div class="text-xl font-bold">GPU 3D Load: <span class="{color}">{load}%</span> <span class="text-sm text-gray-400 ml-4">[{status_text}]</span></div>""")