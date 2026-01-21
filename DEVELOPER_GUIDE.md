# LanEnc Agent Guide

**Attn: Future Coding Agents**  
This document outlines the internal architecture, state management, and potential footguns of the LanEnc project. Read this before making structural changes.

## 🧠 System Context
LanEnc is a **FastAPI + SQLite + FFMPEG** application designed for local video encoding.
It is NOT a standard stateless web app. It maintains significant local state (processing queues, file system locks, subprocess handles) and interacts directly with hardware (GPU).

## 🏗️ Core Architecture & Control Flow

### 1. The "Ghost Process" Killer (`launcher.py`)
*   **Behavior**: On startup, `launcher.py` aggressively checks port 8000.
*   **Risk**: If it finds *any* process on that port, it kills it.
*   **Logic**: It iterates `psutil.process_iter()`, finds the PID bound to the port, and calls `proc.kill()`.
*   **Agent Note**: If you are debugging and the server "randomly" dies or restarts, check if multiple instances of `launcher.py` are fighting.

### 2. State Management (`queue.db` vs `JobManager`)
*   **Database**: `queue.db` is the source of truth for *Job Metadata* (Status, Filename, Config).
*   **Memory**: `JobManager.active_jobs` (Instance in `main.py`) is the source of truth for *Process Handles*.
*   **Synchronization**:
    *   The `worker()` thread (managed by `JobManager`) polls DB `PENDING` items.
    *   When a job starts, it's added to `JobManager.active_jobs`.
    *   When a job finishes/fails, it's removed from `JobManager.active_jobs` and DB is updated.
*   **Danger Zone**: modifying `queue.db` manually (e.g. changing status to `ENCODING`) *without* actually starting a process will cause the UI to hang forever, as no process exists in `JobManager` to update it.

### 3. Concurrency Model
*   **Web Server**: Async (FastAPI/Uvicorn). Handles IO-bound requests (Uploads, DB reads).
*   **Worker**: Synchronous Thread. There is **ONLY ONE** worker thread `t = threading.Thread(target=worker, daemon=True)`.
*   **Implication**: Jobs are strictly serial. Parallel encoding is intentionally disabled to avoid melting the user's GPU.
*   **Future Work**: If adding parallel workers, `gpu_mon.py` logic needs a complete rewrite, as it assumes global GPU ownership.

## 🛠️ Critical Components

### GPU Watchdog (`gpu_mon.py`)
This is the "Gaming Mode" feature.
*   **Mechanism**: Uses `nvidia-ml-py` (pynvml) to poll GPU 3D Usage.
*   **Logic**:
    *   `> 75%`: **PAUSE** encoding (calls `psutil.Process.suspend()`).
    *   `< 60%`: **RESUME** encoding (calls `psutil.Process.resume()`).
*   **Gotcha**: If the Python process crashes while FFMPEG is suspended, the FFMPEG process might become a "zombie" that never resumes. `launcher.py` attempts to clean these up on next run.

### Database Schema (`queue.db`)
*   `queue`:
    *   `id` (PK)
    *   `filename` (Source filename in `uploads/`)
    *   `status`: `PENDING` | `ENCODING` | `COMPLETED` | `FAILED` | `MANUAL_PAUSE`
    *   `progress`: 0-100 (Int)
    *   `elapsed_time`: String (e.g. "5m 20s")
    *   `start_time`: Real (Unix Timestamp)
*   `config`: Key-Value storage for settings (`cq`, `preset`, `resolution`, `codec`).


## 🎥 Encoding Pipeline & Features

### Encoding Modes
The application now supports two distinct encoding strategies:
1.  **Quality (CQ)**: Variable Bitrate based on visual quality (CRF/CQ). `cq` param controls the quality level (18-35).
2.  **Bitrate**: Average Bitrate targeting. `bitrate` param sets the target kbps.

### HEVC Optimizations
When `hevc_nvenc` is selected, specific flags are applied to maximize quality/compression ratio:
*   `-bf 4`: Uses 4 B-frames (better compression).
*   `-b_ref_mode each`: Uses B-frames as references.
*   `-multipass 2` & `-rc-lookahead 32`: Improves rate control accuracy (preset p7).

### GPU Monitoring
The header now displays the specific **GPU Model Name** (e.g., "RTX 3090") alongside live 3D Load.
*   **Source**: `pynvml.nvmlDeviceGetName()`
*   **Updates**: Polled every 2 seconds via HTMX.

## ⚠️ Common Pitfalls

1.  **File Locking**: Windows file locking is aggressive. Deleting a file that FFMPEG is currently writing to will throw `PermissionError`. The `delete` endpoint has a retry loop to mitigate this.
2.  **FFMPEG Binaries**: The app looks for `ffmpeg.exe` in the CWD first, then PATH. Hardcoded paths are avoided to ensure portability.
3.  **Frontend Polling**: The UI uses HTMX (implied by `HX-Trigger` headers in `main.py`), likely polling specific endpoints. When modifying routes, ensure headers like `HX-Trigger: update-queue` are preserved to keep the UI in sync.

## 🧪 Testing Guidelines
*   **No Unit Tests**: Currently, there are no CI/CD pipelines.
*   **Manual Verification**:
    1.  Start server `start_lanenc.bat`.
    2.  Upload a small `.mp4`.
    3.  Check `queue.db` for the new row.
    4.  Watch console for FFMPEG progress.
    5.  Check `processed/` for the `_compressed` output.
