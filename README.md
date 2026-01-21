# LanEnc (LAN Encoder)

LanEnc is a robust, local web-based video encoding server designed to leverage a powerful primary PC (specifically one with an NVIDIA GPU) to compress and transcode video files. It exposes a modern web interface accessible across the local network (LAN), allowing any device to offload intensive video processing tasks.

## 🚀 Features

*   **Remote Offloading**: Upload videos from laptops, tablets, or phones to your main rig for encoding.
*   **Smart Queue Management**: SQlite-backed job queue ensures orderly processing handling multiple requests.
*   **Gaming Mode (GPU Watchdog)**: Automatically pauses encoding tasks when high GPU load (e.g., gaming) is detected and resumes when resources are free.
*   **Hardware Acceleration**: Native support for NVIDIA NVENC (H.264/HEVC/AV1) for high-speed encoding.
*   **Web Dashboard**: Real-time progress tracking, job control (pause/resume/delete), and file management.
*   **Automatic Handbrake**: Acts like a headless, network-accessible Handbrake wrapper.

## 🛠️ Technology Stack

*   **Backend**: Python (FastAPI, Uvicorn)
*   **Frontend**: HTML5, TailwindCSS (via CDN), Jinja2 Templates
*   **Database**: SQLite (`queue.db`)
*   **Processing**: FFMPEG (via `subprocess`), NVIDIA-ML-py (for GPU monitoring)
*   **Process Management**: `psutil` for robust process control and "ghost" process killing.

## 📋 Prerequisites

*   **OS**: Windows 10/11
*   **GPU**: NVIDIA GPU (Required for NVENC and Watchdog features)
*   **Software**:
    *   [Miniconda](https://docs.conda.io/en/latest/miniconda.html) or Anaconda
    *   [Git](https://git-scm.com/)
    *   [FFMPEG](https://ffmpeg.org/download.html) (Included binaries or in system PATH)

## 📦 Installation

1.  **Clone the Repository**
    ```bash
    git clone https://github.com/yourusername/lanEnc.git
    cd lanEnc
    ```

2.  **Environment Setup**
    The included batch script handles environment creation and dependency installation automatically.
    
    Simply run:
    ```cmd
    start_lanenc.bat
    ```

    *Alternatively, manual setup:*
    ```bash
    conda create -n lanenc python=3.9
    conda activate lanenc
    pip install -r requirements.txt
    ```

3.  **Binaries**
    Ensure `ffmpeg.exe` and `ffprobe.exe` are placed in the root directory or are accessible via your system PATH.

## 🚦 Usage

1.  **Start the Server**
    Double-click `start_lanenc.bat`.
    *   The script will check for updates, activate the environment, and launch the server.
    *   It will automatically detect your LAN IP.

2.  **Access the Dashboard**
    *   **Local**: `http://127.0.0.1:8000`
    *   **Network**: `http://<YOUR_LAN_IP>:8000` (The console will display the exact URL).

3.  **Workflow**
    *   **Upload**: Drag and drop video files into the upload area.
    *   **Configure**: Select your desired Codec (e.g., HEVC NVENC), Resolution, and Quality (CRF/CQ).
    *   **Monitor**: Watch the progress bar. The "GPU 3D Load" indicator in the top right shows system health.
    *   **Download**: Once complete, click the file name to download the `_compressed` version.

## ⚙️ Configuration

### Encoder Settings
Settings are persisted in `queue.db`. Defaults can be changed via the Web UI:
*   **Codec**: NVENC H.265 (Recommended), H.264, or AV1 (if supported).
*   **Preset**: Speed vs Quality (Default: `p7` - Best Quality).
*   **CQ (Constant Quality)**: Lower is better quality. Range 18-28 is standard for storage.

### Advanced Config
*   **Port**: Default `8000`. Can be changed in `launcher.py`.
*   **Watchdog Thresholds**: configured in `gpu_mon.py`:
    *   `GAMING_THRESHOLD`: 75% (Pauses encoding)
    *   `RESUME_THRESHOLD`: 60% (Resumes encoding)

## 📂 Project Structure

```
lanEnc/
├── launcher.py         # Entry point, network discovery, and port management
├── main.py             # Core FastAPI application and worker logic
├── gpu_mon.py          # GPU resource monitoring class
├── start_lanenc.bat    # Windows startup and auto-update script
├── requirements.txt    # Python dependencies
├── templates/          # Jinja2 HTML templates
├── uploads/            # Storage for processing files
└── queue.db            # SQLite database (auto-created)
```

## 🤝 Contributing

1.  Fork the Project
2.  Create your Feature Branch (`git checkout -b feature/AmazingFeature`)
3.  Commit your Changes (`git commit -m 'Add some AmazingFeature'`)
4.  Push to the Branch (`git push origin feature/AmazingFeature`)
5.  Open a Pull Request

## 📄 License

[MIT License](LICENSE) (or relevant license)
