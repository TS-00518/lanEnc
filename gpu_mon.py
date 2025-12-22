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
        try:
            pynvml.nvmlInit()
            self.handle = pynvml.nvmlDeviceGetHandleByIndex(GPU_INDEX)
            self.available = True
        except Exception as e:
            logger.error(f"Could not init NVML: {e}")
            self.available = False

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
        if not pid or not psutil.pid_exists(pid):
            return "DEAD"

        proc = psutil.Process(pid)
        load = self.get_3d_load()

        # Check current status
        is_suspended = False
        try:
            # Windows hack to check if suspended
            if proc.status() == psutil.STATUS_STOPPED: 
                is_suspended = True
        except:
            pass

        if load > GAMING_THRESHOLD:
            if not is_suspended:
                logger.warning(f"High GPU Load ({load}%). Pausing FFmpeg...")
                proc.suspend()
            return "PAUSED"
        
        elif load < RESUME_THRESHOLD:
            if is_suspended:
                logger.info(f"GPU Load Normalized ({load}%). Resuming FFmpeg...")
                proc.resume()
            return "RUNNING"
            
        return "PAUSED" if is_suspended else "RUNNING"