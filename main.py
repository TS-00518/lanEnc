import os
import shutil
import subprocess
import threading
import time
import sqlite3
import re
import logging
from typing import List, Optional, Dict, Any
from contextlib import asynccontextmanager, contextmanager

from fastapi import FastAPI, Request, Form, UploadFile, File, Response
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

import psutil

# Internal modules
from gpu_mon import GPUWatchdog

# --- CONFIGURATION & LOGGING ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("LanEnc")

class Settings:
    UPLOAD_DIR: str = "uploads"
    PROCESSED_DIR: str = "processed"
    DEFAULT_CODEC_FALLBACK: str = "hevc_nvenc"
    OUTPUT_SUFFIX: str = "_compressed"
    OUTPUT_EXT: str = ".mp4"
    DB_PATH: str = "queue.db"
    TEMPLATE_DIR: str = "templates"

settings = Settings()

# Ensure directories
os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
os.makedirs(settings.PROCESSED_DIR, exist_ok=True)

# --- DATABASE HELPERS ---

@contextmanager
def get_db_connection():
    """Context manager for SQLite database connections."""
    conn = sqlite3.connect(settings.DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS queue 
                     (id INTEGER PRIMARY KEY, filename TEXT, status TEXT, progress INTEGER, elapsed_time TEXT, start_time REAL)''')
        c.execute('''CREATE TABLE IF NOT EXISTS config 
                     (key TEXT PRIMARY KEY, value TEXT)''')
        
        # Migrations
        try: c.execute("ALTER TABLE queue ADD COLUMN elapsed_time TEXT")
        except sqlite3.OperationalError: pass
        
        try: c.execute("ALTER TABLE queue ADD COLUMN start_time REAL")
        except sqlite3.OperationalError: pass

        conn.commit()

        # Seed Defaults
        defaults = {
            "cq": "24", 
            "bitrate": "4000",
            "mode": "cq", # or 'bitrate'
            "preset": "p7", 
            "resolution": "Original",
            "codec": settings.DEFAULT_CODEC_FALLBACK
        }
        for k, v in defaults.items():
            c.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", (k, v))
        
        # Reset stuck jobs
        c.execute("UPDATE queue SET status='PENDING', progress=0 WHERE status='ENCODING' OR status='MANUAL_PAUSE'")
        conn.commit()

def get_config_value(key: str) -> Optional[str]:
    with get_db_connection() as db:
        row = db.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

def set_config_value(key: str, value: str):
    with get_db_connection() as db:
        db.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, value))
        db.commit()

def update_job_status(job_id: int, status: str, progress: Optional[int] = None, elapsed_time: Optional[str] = None, start_time: Optional[float] = None):
    with get_db_connection() as db:
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

# --- UTILITIES ---

def format_duration(seconds: float) -> str:
    if not seconds: return "0s"
    m, s = divmod(int(seconds), 60)
    if m > 60:
        h, m = divmod(m, 60)
        return f"{h}h {m}m {s}s"
    return f"{m}m {s}s"

class HardwareManager:
    """Manages FFMPEG binaries and Codec detection."""
    def __init__(self):
        self.ffmpeg_bin = "ffmpeg"
        self.ffprobe_bin = "ffprobe"
        self.available_codecs: List[Dict[str, str]] = []

    def check_binaries(self):
        logger.info("Checking binaries...")
        # Check local directory first
        local_ffmpeg = os.path.join(os.getcwd(), "ffmpeg.exe")
        if os.path.exists(local_ffmpeg):
            self.ffmpeg_bin = local_ffmpeg
        elif shutil.which("ffmpeg"):
            self.ffmpeg_bin = "ffmpeg"
        else:
            logger.critical("'ffmpeg' not found in path or local dir.")

        local_ffprobe = os.path.join(os.getcwd(), "ffprobe.exe")
        if os.path.exists(local_ffprobe):
            self.ffprobe_bin = local_ffprobe
        elif shutil.which("ffprobe"):
            self.ffprobe_bin = "ffprobe"
        
        # Fallback guess
        if "ffmpeg.exe" in self.ffmpeg_bin and self.ffprobe_bin == "ffprobe":
            guess = self.ffmpeg_bin.replace("ffmpeg.exe", "ffprobe.exe")
            if os.path.exists(guess):
                self.ffprobe_bin = guess

    def detect_codecs(self):
        logger.info("Detecting hardware codecs...")
        candidates = [
            ("av1_nvenc", "AV1 (NVENC)"),
            ("hevc_nvenc", "H.265/HEVC (NVENC)"),
            ("h264_nvenc", "H.264/AVC (NVENC)")
        ]
        verified = []
        for cid, cname in candidates:
            try:
                cmd = [
                    self.ffmpeg_bin, "-y", "-v", "error", 
                    "-f", "lavfi", "-i", "color=c=black:s=1280x720:d=1",
                    "-c:v", cid, "-pix_fmt", "yuv420p",
                    "-f", "null", "-"
                ]
                # Hide window on Windows
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
        
        self.available_codecs = verified

    def get_video_duration(self, input_path: str) -> float:
        try:
            # Try ffprobe
            if os.path.exists(self.ffprobe_bin) or shutil.which(self.ffprobe_bin):
                cmd = [self.ffprobe_bin, 
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
            logger.debug(f"ffprobe duration failed: {e}")

        # Fallback to ffmpeg
        try:
            cmd = [self.ffmpeg_bin, "-i", input_path]
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, 
                                    universal_newlines=True, startupinfo=startupinfo)
            match = re.search(r"Duration: (\d{2}):(\d{2}):(\d{2}\.\d{2})", result.stderr)
            if match:
                h, m, s = map(float, match.groups())
                return h * 3600 + m * 60 + s
        except Exception as e:
            logger.debug(f"ffmpeg duration fallback failed: {e}")

        return 0.0

# Singleton Hardware Manager
hw_manager = HardwareManager()

# --- JOB MANAGER ---

class JobManager:
    def __init__(self):
        self.active_jobs: Dict[int, subprocess.Popen] = {}
        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None
        self.watchdog = GPUWatchdog()

    def start_worker(self):
        if self._worker_thread and self._worker_thread.is_alive():
            return
        logger.info("Starting Worker Thread...")
        self._stop_event.clear()
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker_thread.start()

    def stop_worker(self):
        logger.info("Stopping Worker Thread...")
        self._stop_event.set()
        if self._worker_thread:
            self._worker_thread.join(timeout=5)

    def kill_job(self, job_id: int):
        if job_id in self.active_jobs:
            try:
                self.active_jobs[job_id].kill()
                self.active_jobs[job_id].wait(timeout=2)
            except Exception:
                pass
            if job_id in self.active_jobs:
                del self.active_jobs[job_id]

    def _worker_loop(self):
        while not self._stop_event.is_set():
            # fetch next job
            try:
                with get_db_connection() as db:
                    job = db.execute("SELECT id, filename FROM queue WHERE status='PENDING' LIMIT 1").fetchone()
            except Exception as e:
                logger.error(f"DB Error in worker: {e}")
                time.sleep(2)
                continue

            if not job:
                time.sleep(2)
                continue

            self._process_job(job)

    def _process_job(self, job):
        job_id = job["id"]
        filename = job["filename"]
        input_path = os.path.join(settings.UPLOAD_DIR, filename)

        if not os.path.exists(input_path):
            logger.error(f"File not found: {input_path}")
            update_job_status(job_id, "FAILED", 0)
            return

        output_filename = os.path.splitext(filename)[0] + settings.OUTPUT_SUFFIX + settings.OUTPUT_EXT
        output_path = os.path.join(settings.PROCESSED_DIR, output_filename)

        total_duration = hw_manager.get_video_duration(input_path)
        
        # Get Config
        mode = get_config_value("mode") or "cq"
        cq = get_config_value("cq") or "24"
        bitrate = get_config_value("bitrate") or "4000"
        preset = get_config_value("preset") or "p7"
        res = get_config_value("resolution") or "Original"
        stored_codec = get_config_value("codec")
        
        valid_codec_ids = [c["id"] for c in hw_manager.available_codecs]
        if stored_codec and stored_codec in valid_codec_ids:
            codec = stored_codec
        elif valid_codec_ids:
            codec = valid_codec_ids[0]
        else:
            codec = settings.DEFAULT_CODEC_FALLBACK

        start_ts = time.time()
        update_job_status(job_id, "ENCODING", 0, start_time=start_ts)

        # Build Command
        cmd = [
            hw_manager.ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
            "-progress", "pipe:1",
            "-i", input_path,
            "-c:v", codec, 
            "-preset", preset,
        ]

        if "nvenc" in codec:
            # HEVC Optimizations
            if "hevc" in codec:
                cmd.extend(["-bf", "4", "-b_ref_mode", "each"])

            if mode == "bitrate":
                # Average Bitrate Mode
                cmd.extend(["-b:v", f"{bitrate}k", "-maxrate", f"{int(bitrate) * 2}k", "-bufsize", f"{int(bitrate) * 4}k"])
            else:
                 # CQ Mode
                cmd.extend(["-rc", "vbr", "-cq", cq, "-b:v", "0"])

            if preset == "p7":
                cmd.extend(["-multipass", "2", "-rc-lookahead", "32", "-spatial-aq", "1", "-temporal-aq", "1"])
        else:
            # Software / non-nvenc fallbacks
            if mode == "bitrate":
                 cmd.extend(["-b:v", f"{bitrate}k"])
            else:
                 cmd.extend(["-crf", cq])

        if res == "2160p": cmd.extend(["-vf", "scale=-1:2160"])
        elif res == "1080p": cmd.extend(["-vf", "scale=-1:1080"])
        elif res == "720p": cmd.extend(["-vf", "scale=-1:720"])
        
        cmd.extend(["-c:a", "copy", output_path])

        try:
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, 
                universal_newlines=True, startupinfo=startupinfo
            )
            
            self.active_jobs[job_id] = proc
            proc_obj = psutil.Process(proc.pid)

            # Monitoring Loop
            while proc.poll() is None:
                if self._stop_event.is_set():
                    proc.kill()
                    break

                # Check status changes (Pause/Delete)
                with get_db_connection() as db:
                    current_status_row = db.execute("SELECT status FROM queue WHERE id=?", (job_id,)).fetchone()
                
                if not current_status_row:
                    proc.kill() # Job deleted
                    break

                status = current_status_row["status"]

                if status == "MANUAL_PAUSE":
                    self._handle_manual_pause(proc_obj)
                    time.sleep(1)
                    continue
                elif status == "ENCODING":
                    self._handle_encoding(proc_obj, proc, total_duration, job_id)
                else:
                    time.sleep(1)

            proc.wait()
            if job_id in self.active_jobs:
                del self.active_jobs[job_id]

            if proc.returncode == 0:
                final_elapsed = format_duration(time.time() - start_ts)
                update_job_status(job_id, "COMPLETED", 100, elapsed_time=final_elapsed)
            elif proc.returncode != 0 and current_status_row:
                update_job_status(job_id, "FAILED", 0)

        except Exception as e:
            logger.error(f"Job Critical Error: {e}")
            update_job_status(job_id, "ERROR", 0)
            if job_id in self.active_jobs:
                del self.active_jobs[job_id]

    def _handle_manual_pause(self, proc_obj):
        try:
            if proc_obj.status() != psutil.STATUS_STOPPED:
                proc_obj.suspend()
        except: pass

    def _handle_encoding(self, proc_obj, proc, total_duration, job_id):
        try:
            if proc_obj.status() == psutil.STATUS_STOPPED:
                proc_obj.resume()
        except: pass
        
        # GPU Watchdog
        watchdog_state = self.watchdog.manage_process(proc.pid)
        
        if watchdog_state == "RUNNING":
            line = proc.stdout.readline()
            if line and "out_time_us=" in line:
                try:
                    val = line.split("=")[1].strip()
                    if val and val != "N/A":
                        us = int(val)
                        if total_duration > 0:
                            percent = int((us / 1000000 / total_duration) * 100)
                            update_job_status(job_id, "ENCODING", percent)
                except: pass

# Global Manager Instance
job_manager = JobManager()

# --- FASTAPI APP ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    init_db()
    hw_manager.check_binaries()
    hw_manager.detect_codecs()
    job_manager.start_worker()
    yield
    # Shutdown
    job_manager.stop_worker()

app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")

templates = Jinja2Templates(directory=settings.TEMPLATE_DIR)

# --- ROUTES ---

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    conf_defaults = {
        "cq": get_config_value("cq") or "24",
        "bitrate": get_config_value("bitrate") or "4000",
        "mode": get_config_value("mode") or "cq",
        "preset": get_config_value("preset") or "p7",
        "resolution": get_config_value("resolution") or "Original",
        "codec": get_config_value("codec") or (hw_manager.available_codecs[0]["id"] if hw_manager.available_codecs else settings.DEFAULT_CODEC_FALLBACK)
    }

    files = []
    if os.path.exists(settings.UPLOAD_DIR):
        try:
            files = sorted([f for f in os.listdir(settings.UPLOAD_DIR) 
                           if f.lower().endswith(('.mkv', '.mp4', '.avi', '.mov', '.webm')) 
                           and settings.OUTPUT_SUFFIX not in f])
        except Exception: pass
    
    queue = []
    with get_db_connection() as db:
        rows = db.execute("SELECT * FROM queue").fetchall()
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
        "settings": conf_defaults,
        "codecs": hw_manager.available_codecs
    })

@app.post("/upload")
async def upload_files(files: List[UploadFile] = File(...)):
    for file in files:
        if not file.filename: continue
        file_location = os.path.join(settings.UPLOAD_DIR, file.filename)
        try:
            with open(file_location, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)
        except Exception as e:
            logger.error(f"Upload failed: {e}")
            if os.path.exists(file_location):
                try: os.remove(file_location)
                except: pass
    return HTMLResponse(content="", headers={"HX-Trigger": "update-files"})

@app.post("/delete_upload")
async def delete_upload(request: Request, filename: str = Form(...)):
    if filename:
        safe_filename = os.path.basename(filename) 
        file_path = os.path.join(settings.UPLOAD_DIR, safe_filename)
        
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
                # We optionally delete the output too? 
                # User asked to separate them, usually deleting source keeps output or asks.
                # For "robustness", let's keep the source delete PURELY source delete.
                # If they want to delete output, they do it from the Queue/History.
                pass
            except Exception as e:
                logger.error(f"Delete error: {e}")
    return await files_list(request)

@app.get("/download/{filename}")
async def download_file(filename: str):
    # Filename here comes from the DB (source filename)
    # We need to construct the output filename
    output_filename = os.path.splitext(filename)[0] + settings.OUTPUT_SUFFIX + settings.OUTPUT_EXT
    file_path = os.path.join(settings.PROCESSED_DIR, output_filename)
    if os.path.exists(file_path):
        return FileResponse(file_path, filename=output_filename)
    return HTMLResponse("File not found.", status_code=404)

@app.get("/files_list", response_class=HTMLResponse)
async def files_list(request: Request):
    files = []
    if os.path.exists(settings.UPLOAD_DIR):
        try:
            files = sorted([f for f in os.listdir(settings.UPLOAD_DIR) 
                           if f.lower().endswith(('.mkv', '.mp4', '.avi', '.mov', '.webm'))
                           and settings.OUTPUT_SUFFIX not in f])
        except Exception: pass
    
    response = templates.TemplateResponse("partials/file_list.html", {"request": request, "files": files})
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response

@app.post("/save_settings")
async def save_settings(
    cq: str = Form(...), 
    bitrate: str = Form("4000"),
    mode: str = Form("cq"),
    preset: str = Form(...), 
    resolution: str = Form(...), 
    codec: str = Form(...)
):
    set_config_value("cq", cq)
    set_config_value("bitrate", bitrate)
    set_config_value("mode", mode)
    set_config_value("preset", preset)
    set_config_value("resolution", resolution)
    set_config_value("codec", codec)
    return HTMLResponse(content="<span class='text-green-400 text-xs ml-2'>Saved!</span>")

@app.post("/add")
async def add_job(filename: str = Form(...)):
    with get_db_connection() as db:
        exists = db.execute("SELECT id FROM queue WHERE filename=? AND status!='COMPLETED'", (filename,)).fetchone()
        if not exists:
            db.execute("INSERT INTO queue (filename, status, progress) VALUES (?, 'PENDING', 0)", (filename,))
            db.commit()
    return HTMLResponse(content="", headers={"HX-Trigger": "update-queue"})

@app.post("/control/{job_id}/{action}")
async def job_control(job_id: int, action: str):
    with get_db_connection() as db:
        if action == "delete":
            job_manager.kill_job(job_id)
            row = db.execute("SELECT filename FROM queue WHERE id=?", (job_id,)).fetchone()
            if row:
                filename = row["filename"]
                output_filename = os.path.splitext(filename)[0] + settings.OUTPUT_SUFFIX + settings.OUTPUT_EXT
                output_path = os.path.join(settings.PROCESSED_DIR, output_filename)
                
                # Try delete active output file
                if os.path.exists(output_path):
                    for _ in range(3):
                        try:
                            os.remove(output_path)
                            break
                        except PermissionError:
                            time.sleep(0.5)
            db.execute("DELETE FROM queue WHERE id=?", (job_id,))

        elif action == "pause":
            db.execute("UPDATE queue SET status='MANUAL_PAUSE' WHERE id=?", (job_id,))
        elif action == "resume":
            db.execute("UPDATE queue SET status='PENDING' WHERE status='MANUAL_PAUSE' AND id=?", (job_id,))
            db.execute("UPDATE queue SET status='ENCODING' WHERE id=?", (job_id,))
        
        db.commit()

    return HTMLResponse(content="", headers={"HX-Trigger": "update-queue"})

@app.get("/status_bar")
async def status_bar():
    load = job_manager.watchdog.get_3d_load()
    name = job_manager.watchdog.get_device_name()
    color = "text-emerald-400" if load < 75 else "text-amber-400"
    
    return HTMLResponse(f"""
        <div class="flex items-center gap-4 text-sm font-medium text-white/90">
             <div class="flex items-center gap-2 text-slate-300 bg-white/10 px-3 py-1.5 rounded-lg border border-white/5 shadow-sm">
                <i class="fas fa-microchip text-indigo-400"></i>
                <span class="tracking-wide">{name}</span>
            </div>
            <div class="flex items-center gap-2 bg-white/10 px-3 py-1.5 rounded-lg border border-white/5 shadow-sm">
                <span class="text-slate-300">3D Load:</span>
                <span class="{color} font-bold font-mono">{load}%</span>
            </div>
        </div>
    """)