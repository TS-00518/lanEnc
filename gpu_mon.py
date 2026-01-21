import pynvml
import psutil
import time
import logging

# --- CONFIG ---
GPU_INDEX = 0
GAMING_THRESHOLD = 75  # Stop if usage > 75%
RESUME_THRESHOLD = 60  # Resume if usage < 60%

logger = logging.getLogger("watchdog")

class GPUWatchdog:
    def __init__(self):
        # Track which PIDs we have explicitly paused
        self._paused_pids = set()
        self.device_name = "Unknown GPU"
        
        try:
            pynvml.nvmlInit()
            self.handle = pynvml.nvmlDeviceGetHandleByIndex(GPU_INDEX)
            self.available = True
            try:
                name = pynvml.nvmlDeviceGetName(self.handle)
                if isinstance(name, bytes):
                    self.device_name = name.decode('utf-8')
                else:
                    self.device_name = str(name)
                logger.info(f"GPU Detected: {self.device_name}")
            except Exception as e:
                logger.warning(f"Could not get GPU Name: {e}")
                self.device_name = "NVIDIA GPU"
        except Exception as e:
            logger.error(f"Could not init NVML: {e}")
            self.available = False

    def get_device_name(self):
        return self.device_name

    def get_3d_load(self):
        if not self.available: return 0
        try:
            util = pynvml.nvmlDeviceGetUtilizationRates(self.handle)
            return util.gpu  # This is the 3D/Compute load
        except:
            return 0

    def manage_process(self, pid):
        """
        Checks GPU load and Pauses/Resumes the FFmpeg PID.
        Returns: 'RUNNING' | 'PAUSED' | 'KILLED'
        """
        # Cleanup if process is dead
        if not pid or not psutil.pid_exists(pid):
            if pid in self._paused_pids:
                self._paused_pids.remove(pid)
            return "DEAD"

        proc = psutil.Process(pid)
        load = self.get_3d_load()

        # CASE 1: Gaming detected -> PAUSE
        if load > GAMING_THRESHOLD:
            # Only suspend if we haven't already tracked it as paused
            if pid not in self._paused_pids:
                logger.warning(f"High GPU Load ({load}%). Pausing FFmpeg...")
                try:
                    proc.suspend()
                    self._paused_pids.add(pid)
                except Exception as e:
                    logger.error(f"Failed to suspend: {e}")
            return "PAUSED"
        
        # CASE 2: GPU is free -> RESUME
        elif load < RESUME_THRESHOLD:
            # Only resume if WE paused it (prevents spamming resume calls)
            if pid in self._paused_pids:
                logger.info(f"GPU Load Normalized ({load}%). Resuming FFmpeg...")
                try:
                    proc.resume()
                    self._paused_pids.remove(pid)
                except Exception as e:
                    logger.error(f"Failed to resume: {e}")
            return "RUNNING"
            
        # CASE 3: Hysteresis zone (60-75%) -> Maintain current state
        return "PAUSED" if pid in self._paused_pids else "RUNNING"