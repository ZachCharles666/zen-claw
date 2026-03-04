import subprocess
import time
import sys
import threading
import urllib.request
import urllib.error
import hmac
import hashlib
import os

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8')

def run_cmd(cmd, check=True):
    print(f"\n[+] Running: {' '.join(cmd)}")
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', env=env)
    if check and result.returncode != 0:
        print(f"[!] Error running command\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}")
        return False
    else:
        print(result.stdout.strip())
        return True

def test_cli_basics():
    print("\n--- Testing CLI Basics ---")
    commands = [
        [sys.executable, "-m", "zen_claw", "--help"],
        [sys.executable, "-m", "zen_claw", "config", "providers"],
        [sys.executable, "-m", "zen_claw", "config", "doctor"],
    ]
    for cmd in commands:
        if not run_cmd(cmd):
            print("CLI Basics test failed.")
            return False
    return True

def test_agent_interaction():
    print("\n--- Testing Agent Chat ---")
    # Due to token cost and API key requirements which might not be set in this env, 
    # we'll test a simple help/status or isolated skill test if available.
    # We will test the status command which exercises the framework
    if not run_cmd([sys.executable, "-m", "zen_claw", "status", "-v"]):
        return False
        
    print("[+] Simulating TPM20 rate limits... sleeping for 3.5 seconds.")
    time.sleep(3.5)
    
    return True

def check_venv():
    if sys.prefix == sys.base_prefix:
        print("[!] Error: This test script must be run inside an activated virtual environment.")
        print("    Please run `.\\.venv\\Scripts\\Activate.ps1` (Windows) or `source .venv/bin/activate` (Mac/Linux) first.\n")
        sys.exit(1)

def simulate_webhook_traffic():
    print("\n--- Testing Webhook Gateway Security ---")
    
    import json
    config_path = os.path.expanduser("~/.zen-claw/config.json")
    original_config = None
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            original_config = f.read()
            config_data = json.loads(original_config)
    else:
        config_data = {}

    if "channels" not in config_data:
        config_data["channels"] = {}
    if "webhook_trigger" not in config_data["channels"]:
        config_data["channels"]["webhook_trigger"] = {}
        
    config_data["channels"]["webhook_trigger"]["enabled"] = True
    config_data["channels"]["webhook_trigger"]["secret"] = "fake_secret"
    
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config_data, f, indent=2)

    # NOTE: `zen_claw dashboard --port N` runs ThreadingHTTPServer (no FastAPI routes).
    # The FastAPI api_app (with /webhook/trigger/) must be served via uvicorn directly.
    # We also need to register the WebhookTriggerChannel so the route doesn't return 503.
    import tempfile, textwrap
    helper_script = textwrap.dedent("""\
        import sys
        sys.path.insert(0, r'{src_dir}')
        import uvicorn
        from zen_claw.config.loader import load_config
        from zen_claw.bus.queue import MessageBus
        from zen_claw.channels.webhook_trigger import WebhookTriggerChannel
        from zen_claw.dashboard.webhooks import register_channels
        from zen_claw.dashboard.server import api_app

        cfg = load_config()
        bus = MessageBus()
        wt_channel = WebhookTriggerChannel(cfg.channels.webhook_trigger, bus)
        register_channels(webhook_trigger=wt_channel)

        uvicorn.run(api_app, host="127.0.0.1", port=19999, log_level="error")
    """.format(src_dir=os.path.dirname(os.path.abspath(__file__)).replace("\\", "\\\\")))

    # Write to a temp file
    helper_path = os.path.join(tempfile.gettempdir(), "_nc_e2e_uvicorn_helper.py")
    with open(helper_path, "w", encoding="utf-8") as f:
        f.write(helper_script)

    print("[+] Starting FastAPI api_app via uvicorn (port 19999)...")
    import socket
    try:
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        gw_process = subprocess.Popen(
            [sys.executable, helper_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=os.path.dirname(os.path.abspath(__file__))
        )
        
        startup_timeout = 15
        start_time = time.time()
        port_open = False
        
        print("[+] Waiting for Webhook Gateway port to open...")
        while time.time() - start_time < startup_timeout:
            if gw_process.poll() is not None:
                print("[!] Gateway process died unexpectedly before port opened.")
                return False
                
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1.0)
                result = s.connect_ex(('127.0.0.1', 19999))
                if result == 0:
                    port_open = True
                    break
            time.sleep(1.0)
            
        if not port_open:
            print(f"[!] Target port 19999 did not open within {startup_timeout} seconds.")
            gw_process.terminate()
            return False
            
        print("[+] Port is open! Proceeding with Webhook Security Test...")
        time.sleep(1) # Allow HTTP server to fully bind
        
        success = True
        try:
            url = "http://127.0.0.1:19999/webhook/trigger/test_agent"
            payload = b'{"text": "hello"}'
            timestamp = str(int(time.time()))
            nonce = "sim_nonce_" + str(int(time.time()))
            
            # Intentionally sign with WRONG secret to trigger the 403 block.
            # The server uses "fake_secret", so signing with "bad_secret" should be rejected.
            wrong_secret = b"bad_secret"
            message = f"{timestamp}.{nonce}.".encode("utf-8") + payload
            signature = hmac.new(wrong_secret, message, hashlib.sha256).hexdigest()
            
            headers = {
                "Content-Type": "application/json",
                "X-Signature": signature,
                "X-Timestamp": timestamp,
                "X-Nonce": nonce
            }
            
            proxy_handler = urllib.request.ProxyHandler({})
            opener = urllib.request.build_opener(proxy_handler)
            req = urllib.request.Request(url, data=payload, headers=headers)
            
            try:
                r = opener.open(req, timeout=5.0)
                status_code = r.getcode()
            except urllib.error.HTTPError as e:
                status_code = e.code
            except Exception as e:
                print(f"[!] Request failed: {e}")
                status_code = 0
                
            print(f"Gateway Response: {status_code}")
            if status_code == 403:
                print("[+] Security Guard works! Unauthorized webhook blocked with 403 as expected.")
            else:
                print(f"[!] Unexpected status code: {status_code} (expected 403 for bad signature)")
                success = False
                
        except Exception as e:
            print(f"[!] Unexpected Request error: {e}")
            success = False
    finally:
        print("[+] Terminating Gateway...")
        gw_process.terminate()
        try:
            gw_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            gw_process.kill()
            
        # Restore configuration
        if original_config is not None:
            with open(config_path, "w", encoding="utf-8") as f:
                f.write(original_config)
        else:
            os.remove(config_path)
            
    return success

def main():
    print("=========================================")
    print(" Zen-Claw V7 Full E2E Simulation Script ")
    print("=========================================")
    
    check_venv()
    
    steps = [
        test_cli_basics,
        test_agent_interaction,
        simulate_webhook_traffic
    ]
    
    all_passed = True
    for step in steps:
        if not step():
            all_passed = False
            break
            
    print("\n=========================================")
    if all_passed:
        print(" [SUCCESS] All simulation tests passed! ")
    else:
        print(" [FAILED] Some simulation tests failed. ")
    print("=========================================")
    
    sys.exit(0 if all_passed else 1)

if __name__ == "__main__":
    main()
