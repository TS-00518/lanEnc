import uvicorn
import os
import socket

def get_local_ip():
    try:
        # Connect to a dummy external IP to determine the local interface IP
        s = socket.socket(socket.socket.AF_INET, socket.socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except:
        return "127.0.0.1"

if __name__ == "__main__":
    # Clear console
    os.system('cls' if os.name == 'nt' else 'clear')
    
    ip = get_local_ip()
    port = 8000
    
    print("="*60)
    print(f"   🚀 LanEnc Server Starting...")
    print(f"   👉 ACCESS UI HERE: http://{ip}:{port}")
    print("="*60)
    print("\n")
    
    # Run the server on 0.0.0.0 to allow LAN access
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False, workers=1)