from .alert_system.bp import alerts_bp

def send_alert(result):
    print("[alert] Sending alert.. ", flush=True)
    if result["alert_needed"]:
        print("[alert] Found leaked data:", result["details"], flush=True)