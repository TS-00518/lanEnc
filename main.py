import os
import shutil
import subprocess
import threading
import time
import sqlite3
import psutil
import re
from typing import List
from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from gpu_mon import GPUWatchdog

# --- CONFIGURATION ---
UPLOAD_DIR = "uploads"
DEFAULT_CODEC_FALLBACK = "hevc_nvenc" 
OUTPUT_SUFFIX = "_compressed"
OUTPUT_EXT = ".mp4" 

# Ensure upload directory exists
os.makedirs(UPLOAD_DIR, exist_ok=True)

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

            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, 
                                    universal_newlines=True, startupinfo=startupinfo)
            
            val = result.stdout.strip()
            if val and val != "N/A":
                return float(val)
    except Exception as e:
        print(f"DEBUG: ffprobe failed: {e}")

    try:
        cmd = [FFMPEG_BIN, "-i", input_path]
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, 
                                universal_newlines=True, startupinfo=startupinfo)
        
        match = re.search(r"Duration: (\d{2}):(\d{2}):(\d{2}\.\d{2})", result.stderr)
        if match:
            hours, minutes, seconds = map(float, match.groups())
            return hours * 3600 + minutes * 60 + seconds
    except Exception as e:
        print(f"DEBUG: ffmpeg fallback failed: {e}")

    return 0

# --- STARTUP CHECKS ---
def check_binaries():
    global FFMPEG_BIN, FFPROBE_BIN
    print("--- STARTUP CHECK ---")
    
    local_ffmpeg = os.path.join(os.getcwd(), "ffmpeg.exe")
    if os.path.exists(local_ffmpeg):
        FFMPEG_BIN = local_ffmpeg
    elif shutil.which("ffmpeg"):
        FFMPEG_BIN = "ffmpeg"
    else:
        print("❌ CRITICAL: 'ffmpeg' not found.")

    local_ffprobe = os.path.join(os.getcwd(), "ffprobe.exe")
    if os.path.exists(local_ffprobe):
        FFPROBE_BIN = local_ffprobe
    elif shutil.which("ffprobe"):
        FFPROBE_BIN = "ffprobe"
    else:
        if "ffmpeg.exe" in FFMPEG_BIN:
            guess = FFMPEG_BIN.replace("ffmpeg.exe", "ffprobe.exe")
            if os.path.exists(guess):
                 FFPROBE_BIN = guess

def detect_hardware_codecs():
    global AVAILABLE_CODECS, FFMPEG_BIN
    print("--- DETECTING HARDWARE SUPPORT ---")
    
    candidates = [
        ("av1_nvenc", "AV1 (NVENC)"),
        ("hevc_nvenc", "H.265/HEVC (NVENC)"),
        ("h264_nvenc", "H.264/AVC (NVENC)")
    ]
    
    verified = []
    
    for cid, cname in candidates:
        try:
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
                verified.append({"id": cid, "name": cname})
        except Exception:
            pass
            
    if not verified:
        verified.append({"id": "libx265", "name": "H.265 (CPU - Slow)"})
        
    AVAILABLE_CODECS = verified

check_binaries()
detect_hardware_codecs()

# Database Setup
def init_db():
    conn = sqlite3.connect("queue.db", check_same_thread=False)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS queue 
                 (id INTEGER PRIMARY KEY, filename TEXT, status TEXT, progress INTEGER, elapsed_time TEXT, start_time REAL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS config 
                 (key TEXT PRIMARY KEY, value TEXT)''')
    
    try: c.execute("ALTER TABLE queue ADD COLUMN elapsed_time TEXT")
    except sqlite3.OperationalError: pass 
    
    try: c.execute("ALTER TABLE queue ADD COLUMN start_time REAL")
    except sqlite3.OperationalError: pass

    best_codec = AVAILABLE_CODECS[0]["id"] if AVAILABLE_CODECS else DEFAULT_CODEC_FALLBACK

    defaults = {
        "cq": "24", 
        "preset": "p7", 
        "resolution": "Original",
        "codec": best_codec
    }
    
    for k, v in defaults.items():
        c.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", (k, v))

    c.execute("UPDATE config SET value='p7' WHERE key='preset' AND value='p6'")

    c.execute("UPDATE queue SET status='PENDING', progress=0 WHERE status='ENCODING' OR status='MANUAL_PAUSE'")
    conn.commit()
    conn.close()

init_db()

def get_db():
    conn = sqlite3.connect("queue.db", check_same_thread=False)
    conn.row_factory = sqlite3.Row 
    return conn

def get_config_value(key):
    db = get_db()
    row = db.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    db.close()
    return row["value"] if row else None

def set_config_value(key, value):
    db = get_db()
    db.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, value))
    db.commit()
    db.close()

def update_job_status(job_id, status, progress=None, elapsed_time=None, start_time=None):
    db = get_db()
    params = [status]
    query = "UPDATE queue SET status=?"
    
    if progress is not None:
        query += ", progress=?"
        params.append(progress)
    
    if elapsed_time is not None:
        query += ", elapsed_time=?"
        params.append(elapsed_time)
        
    if start_time is not None:
        query += ", start_time=?"
        params.append(start_time)

    query += " WHERE id=?"
    params.append(job_id)
    
    db.execute(query, tuple(params))
    db.commit()
    db.close()

def format_duration(seconds):
    if not seconds: return "0s"
    m, s = divmod(int(seconds), 60)
    if m > 60:
        h, m = divmod(m, 60)
        return f"{h}h {m}m {s}s"
    return f"{m}m {s}s"

# --- WORKER LOOP ---
def worker():
    global ACTIVE_JOBS
    while True:
        db = get_db()
        job = db.execute("SELECT id, filename FROM queue WHERE status='PENDING' LIMIT 1").fetchone()
        db.close()

        if not job:
            time.sleep(2)
            continue

        job_id = job["id"]
        filename = job["filename"]
        input_path = os.path.join(UPLOAD_DIR, filename)
        
        if not os.path.exists(input_path):
            print(f"ERROR: File not found {input_path}")
            update_job_status(job_id, "FAILED", 0)
            continue

        output_filename = os.path.splitext(filename)[0] + OUTPUT_SUFFIX + OUTPUT_EXT
        output_path = os.path.join(UPLOAD_DIR, output_filename)

        total_duration = get_video_duration(input_path)

        cq = get_config_value("cq") or "24"
        preset = get_config_value("preset") or "p7"
        res = get_config_value("resolution") or "Original"
        
        stored_codec = get_config_value("codec")
        valid_codec_ids = [c["id"] for c in AVAILABLE_CODECS]
        
        if stored_codec and stored_codec in valid_codec_ids:
            codec = stored_codec
        elif valid_codec_ids:
            codec = valid_codec_ids[0]
        else:
            codec = DEFAULT_CODEC_FALLBACK

        start_ts = time.time()
        update_job_status(job_id, "ENCODING", 0, start_time=start_ts)

        cmd = [
            FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error",
            "-progress", "pipe:1",
            "-i", input_path,
            "-c:v", codec, 
            "-preset", preset,
        ]

        if "nvenc" in codec:
            cmd.extend(["-rc", "vbr", "-cq", cq, "-b:v", "0"])
            
            if preset == "p7":
                cmd.extend([
                    "-multipass", "2", 
                    "-rc-lookahead", "32",
                    "-spatial-aq", "1",
                    "-temporal-aq", "1"
                ])
        else:
            cmd.extend(["-crf", cq])

        if res == "1080p": cmd.extend(["-vf", "scale=-1:1080"])
        elif res == "720p": cmd.extend(["-vf", "scale=-1:720"])
        
        cmd.extend(["-c:a", "copy", output_path])

        try:
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, 
                universal_newlines=True, startupinfo=startupinfo
            )
            
            ACTIVE_JOBS[job_id] = proc
            proc_obj = psutil.Process(proc.pid)
            
            current_status_row = None 
            
            while proc.poll() is None:
                db = get_db()
                current_status_row = db.execute("SELECT status FROM queue WHERE id=?", (job_id,)).fetchone()
                db.close()

                if not current_status_row:
                    proc.kill()
                    break
                
                status = current_status_row["status"]

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

            proc.wait()

            if job_id in ACTIVE_JOBS:
                del ACTIVE_JOBS[job_id]

            if proc.returncode == 0:
                final_elapsed = format_duration(time.time() - start_ts)
                update_job_status(job_id, "COMPLETED", 100, elapsed_time=final_elapsed)
            elif proc.returncode != 0 and current_status_row: 
                update_job_status(job_id, "FAILED", 0)

        except Exception as e:
            print(f"CRITICAL ERROR: {e}")
            update_job_status(job_id, "ERROR", 0)
            if job_id in ACTIVE_JOBS:
                del ACTIVE_JOBS[job_id]

t = threading.Thread(target=worker, daemon=True)
t.start()

# --- ROUTES ---

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    settings = {
        "cq": get_config_value("cq") or "24",
        "preset": get_config_value("preset") or "p7",
        "resolution": get_config_value("resolution") or "Original",
        "codec": get_config_value("codec") or (AVAILABLE_CODECS[0]["id"] if AVAILABLE_CODECS else DEFAULT_CODEC_FALLBACK)
    }

    files = []
    if os.path.exists(UPLOAD_DIR):
        try:
            files = sorted([f for f in os.listdir(UPLOAD_DIR) 
                           if f.lower().endswith(('.mkv', '.mp4', '.avi', '.mov', '.webm')) 
                           and OUTPUT_SUFFIX not in f])
        except Exception: pass
    
    db = get_db()
    rows = db.execute("SELECT * FROM queue").fetchall()
    db.close()
    
    queue = []
    current_time = time.time()
    for row in rows:
        r = dict(row)
        if r["status"] == "ENCODING" and r["start_time"]:
            r["elapsed_time"] = format_duration(current_time - r["start_time"])
        queue.append(r)
    
    return templates.TemplateResponse("index.html", {
        "request": request, 
        "files": files, 
        "queue": queue, 
        "settings": settings,
        "codecs": AVAILABLE_CODECS
    })

@app.post("/upload")
async def upload_files(files: List[UploadFile] = File(...)):
    if not os.path.exists(UPLOAD_DIR):
        os.makedirs(UPLOAD_DIR)
        
    for file in files:
        if not file.filename: continue
        file_location = os.path.join(UPLOAD_DIR, file.filename)
        with open(file_location, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
            
    return RedirectResponse(url="/", status_code=303)

@app.post("/delete_upload")
async def delete_upload(request: Request, filename: str = Form(...)):
    if filename:
        safe_filename = os.path.basename(filename) 
        file_path = os.path.join(UPLOAD_DIR, safe_filename)
        
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
                output_name = os.path.splitext(safe_filename)[0] + OUTPUT_SUFFIX + OUTPUT_EXT
                output_path = os.path.join(UPLOAD_DIR, output_name)
                if os.path.exists(output_path):
                    os.remove(output_path)
            except Exception as e:
                print(f"Error deleting file: {e}")
            
    return await files_list(request)

@app.get("/download/{filename}")
async def download_file(filename: str):
    output_filename = os.path.splitext(filename)[0] + OUTPUT_SUFFIX + OUTPUT_EXT
    file_path = os.path.join(UPLOAD_DIR, output_filename)
    
    if os.path.exists(file_path):
        return FileResponse(file_path, filename=output_filename)
    return HTMLResponse("File not found or encoding not complete.", status_code=404)

@app.get("/files_list", response_class=HTMLResponse)
async def files_list(request: Request):
    files = []
    if os.path.exists(UPLOAD_DIR):
        try:
            files = sorted([f for f in os.listdir(UPLOAD_DIR) 
                           if f.lower().endswith(('.mkv', '.mp4', '.avi', '.mov', '.webm'))
                           and OUTPUT_SUFFIX not in f])
        except Exception: pass
    
    response = templates.TemplateResponse("partials/file_list.html", {"request": request, "files": files})
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response

@app.post("/save_settings")
async def save_settings(cq: str = Form(...), preset: str = Form(...), resolution: str = Form(...), codec: str = Form(...)):
    set_config_value("cq", cq)
    set_config_value("preset", preset)
    set_config_value("resolution", resolution)
    set_config_value("codec", codec)
    return HTMLResponse(content="<span class='text-green-400 text-xs ml-2'>Saved!</span>")

@app.post("/add")
async def add_job(filename: str = Form(...)):
    db = get_db()
    exists = db.execute("SELECT id FROM queue WHERE filename=? AND status!='COMPLETED'", (filename,)).fetchone()
    if not exists:
        db.execute("INSERT INTO queue (filename, status, progress) VALUES (?, 'PENDING', 0)", (filename,))
        db.commit()
    db.close()
    return HTMLResponse(content="", headers={"HX-Trigger": "update-queue"})

@app.post("/control/{job_id}/{action}")
async def job_control(job_id: int, action: str):
    db = get_db()
    
    if action == "delete":
        # 1. Kill Process FIRST
        if job_id in ACTIVE_JOBS:
            try: 
                ACTIVE_JOBS[job_id].kill()
                # Wait briefly for process to die and release file handle
                ACTIVE_JOBS[job_id].wait(timeout=2) 
            except: pass
            
        # 2. Get details and delete files
        row = db.execute("SELECT filename FROM queue WHERE id=?", (job_id,)).fetchone()
        if row:
            filename = row["filename"]
            output_filename = os.path.splitext(filename)[0] + OUTPUT_SUFFIX + OUTPUT_EXT
            output_path = os.path.join(UPLOAD_DIR, output_filename)
            
            # Now try to delete the unfinished file
            if os.path.exists(output_path):
                try: 
                    # Add a small retry loop for file locks
                    for _ in range(3):
                        try:
                            os.remove(output_path)
                            break
                        except PermissionError:
                            time.sleep(0.5)
                except: pass
        
        # 3. Remove from DB
        db.execute("DELETE FROM queue WHERE id=?", (job_id,))

    elif action == "pause":
        db.execute("UPDATE queue SET status='MANUAL_PAUSE' WHERE id=?", (job_id,))
    elif action == "resume":
        db.execute("UPDATE queue SET status='PENDING' WHERE status='MANUAL_PAUSE' AND id=?", (job_id,))
        db.execute("UPDATE queue SET status='ENCODING' WHERE id=?", (job_id,))
        
    db.commit()
    db.close()
    return HTMLResponse(content="", headers={"HX-Trigger": "update-queue"})

@app.get("/status_bar")
async def status_bar():
    load = watchdog.get_3d_load()
    color = "text-green-500" if load < 75 else "text-red-500"
    status_text = "Idle (Encoding)" if load < 75 else "Gaming (Paused)"
    return HTMLResponse(f"""<div class="text-xl font-bold">GPU 3D Load: <span class="{color}">{load}%</span> <span class="text-sm text-gray-400 ml-4">[{status_text}]</span></div>""")