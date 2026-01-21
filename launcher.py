import uvicorn
import os
import socket
import psutil
import time
import shutil
import sys

def get_local_ip():
    # Method 1: Connect to external server (Best for LAN IP)
    try:
        s = socket.socket(socket.socket.AF_INET, socket.socket.SOCK_DGRAM)
        # Doesn't actually connect, just determines route
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        pass

    # Method 2: Fallback to hostname
    try:
        return socket.gethostbyname(socket.gethostname())
    except:
        return "127.0.0.1"

def wait_for_port_release(port, timeout=3.0):
    """
    Waits until the port is actually free.
    Returns True if free, False if still busy after timeout.
    """
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            # Try to bind to the port. If successful, it's free.
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(('0.0.0.0', port))
                return True
        except OSError:
            time.sleep(0.1)
    return False

def kill_port_hog(port):
    """
    Detects if a process is holding the target port and kills it efficiently.
    """
    killed = False
    for proc in psutil.process_iter(['pid', 'name']):
        try:
            for conn in proc.net_connections(kind='inet'):
                if conn.laddr.port == port:
                    print(f"   ⚠️  Port {port} is busy. Killing ghost process: {proc.info['name']} (PID: {proc.info['pid']})")
                    proc.kill()
                    proc.wait(timeout=3) # Wait for it to actually die
                    killed = True
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
        except psutil.TimeoutExpired:
            print(f"   ⚠️  Warning: Process {proc.pid} taking too long to terminate.")
            pass

    if killed:
        print("   ✅  Port cleared. Verifying release...")
        if wait_for_port_release(port):
             print("   ✅  Socket is free.")
        else:
             print("   ❌  Socket still busy (TIME_WAIT state?). Retrying anyway...")

def check_dependencies():
    """Checks for critical external dependencies."""
    if not shutil.which("ffmpeg") and not os.path.exists("ffmpeg.exe"):
        print("\n❌ CRITICAL ERROR: 'ffmpeg' not found.")
        print("   Please ensure ffmpeg is installed and added to your PATH, or placed in this folder.")
        print("   Download: https://ffmpeg.org/download.html")
        input("\n   Press Enter to exit...")
        sys.exit(1)

if __name__ == "__main__":
    # Clear console
    os.system('cls' if os.name == 'nt' else 'clear')
    
    # 1. Check Env
    check_dependencies()

    lan_ip = get_local_ip()
    port = 8000
    
    print("="*60)
    print(f"   🚀 LanEnc Server Starting...")
    
    # 2. Port Hygiene
    kill_port_hog(port)

    print(f"   💻 Local: http://127.0.0.1:{port}")
    if lan_ip != "127.0.0.1":
        print(f"   🌐 LAN:   http://{lan_ip}:{port}")
    print("="*60)
    print("\n")
    
    try:
        # Run the server on 0.0.0.0 to allow LAN access
        config = uvicorn.Config("main:app", host="0.0.0.0", port=port, reload=False, workers=1, log_level="info")
        server = uvicorn.Server(config)
        server.run()
    except OSError as e:
        if "10048" in str(e):
            print("\n❌ ERROR: Port 8000 is still locked by the OS or an Admin process.")
            print("   Please wait a few seconds and try again, or restart your PC.")
            input("   Press Enter to exit...")
        else:
            raise e
    except KeyboardInterrupt:
        print("\n   🛑 Server stopped by user.")