import uvicorn
import os
import socket

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

if __name__ == "__main__":
    # Clear console
    os.system('cls' if os.name == 'nt' else 'clear')
    
    lan_ip = get_local_ip()
    port = 8000
    
    print("="*60)
    print(f"   🚀 LanEnc Server Starting...")
    print(f"   💻 Local: http://127.0.0.1:{port}")
    if lan_ip != "127.0.0.1":
        print(f"   🌐 LAN:   http://{lan_ip}:{port}")
    print("="*60)
    print("\n")
    
    # Run the server on 0.0.0.0 to allow LAN access
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False, workers=1)