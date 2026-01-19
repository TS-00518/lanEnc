import uvicorn
import os
import socket
import psutil
import time
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

def kill_port_hog(port):
    """
    Detects if a process is holding the target port and kills it.
    """
    killed = False
    for proc in psutil.process_iter(['pid', 'name']):
        try:
            for conn in proc.connections(kind='inet'):
                if conn.laddr.port == port:
                    print(f"   ⚠️  Port {port} is busy. Killing ghost process: {proc.info['name']} (PID: {proc.info['pid']})")
                    proc.kill()
                    killed = True
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
    
    if killed:
        print("   ✅  Port cleared. Waiting for release...")
        time.sleep(2) # Give the OS a moment to release the socket

if __name__ == "__main__":
    # Clear console
    os.system('cls' if os.name == 'nt' else 'clear')
    
    lan_ip = get_local_ip()
    port = 8000
    
    print("="*60)
    print(f"   🚀 LanEnc Server Starting...")
    
    # --- AUTO-FIX: KILL GHOST PROCESSES ---
    kill_port_hog(port)

    print(f"   💻 Local: http://127.0.0.1:{port}")
    if lan_ip != "127.0.0.1":
        print(f"   🌐 LAN:   http://{lan_ip}:{port}")
    print("="*60)
    print("\n")
    
    try:
        # Run the server on 0.0.0.0 to allow LAN access
        uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False, workers=1)
    except OSError as e:
        if "10048" in str(e):
            print("\n❌ ERROR: Port 8000 is still locked by the OS or an Admin process.")
            print("   Please wait a few seconds and try again, or restart your PC.")
            input("   Press Enter to exit...")
        else:
            raise e