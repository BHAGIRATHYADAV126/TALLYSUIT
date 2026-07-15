"""
Tally Suite — Tally Bridge Agent
======================================================
Run this on the customer's machine (same PC as Tally Prime).
It connects outward to your server via WebSocket so the
server can push XML into Tally Prime without any port
forwarding on the customer side.

Build into .exe:
  pip install pyinstaller websockets httpx
  pyinstaller --onefile --noconsole --name tally_bridge tally_bridge.py

Usage:
  tally_bridge.exe --server wss://yourdomain.com/ws/tally --key LICENCE-KEY
  (or double-click after configuring config.json)

How it works:
  1. Connects to wss://yourdomain.com/ws/tally/{machine_id}
  2. Server sends XML payloads over WebSocket
  3. Bridge POSTs XML to localhost:9000 (Tally Prime)
  4. Returns Tally's response back to server
"""

import asyncio
import json
import sys
import os
import argparse
import hashlib
import platform
import socket
from pathlib import Path

try:
    import websockets
    import httpx
except ImportError:
    print("Missing packages. Run: pip install websockets httpx")
    sys.exit(1)

# ── Config ────────────────────────────────────────────────────────────────────
CONFIG_FILE  = Path(os.path.dirname(sys.executable)) / "config.json"
TALLY_URL    = "http://localhost:9900"
RECONNECT_DELAY = 5   # seconds between reconnect attempts

def load_config() -> dict:
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {}

def save_config(cfg: dict):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

def get_machine_id() -> str:
    """Generate a stable machine ID from hardware info."""
    raw = "|".join([
        platform.node(),
        platform.machine(),
        platform.processor(),
        socket.gethostname(),
    ])
    return hashlib.sha256(raw.encode()).hexdigest()[:32]

# ── Tally communication ───────────────────────────────────────────────────────
async def send_to_tally(xml: str, tally_url: str = TALLY_URL) -> dict:
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                tally_url,
                content=xml.encode("utf-8"),
                headers={"Content-Type": "text/xml; charset=utf-8"},
            )
        return {"success": True, "status_code": resp.status_code, "body": resp.text}
    except httpx.ConnectError:
        return {"success": False, "error": f"Tally not reachable at {tally_url}. Is Tally Prime open?"}
    except Exception as e:
        return {"success": False, "error": str(e)}

async def ping_tally(tally_url: str = TALLY_URL) -> bool:
    ping = """<ENVELOPE><HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>
<BODY><EXPORTDATA><REQUESTDESC><REPORTNAME>List of Companies</REPORTNAME>
</REQUESTDESC></EXPORTDATA></BODY></ENVELOPE>"""
    result = await send_to_tally(ping, tally_url)
    return result["success"]

# ── WebSocket bridge ──────────────────────────────────────────────────────────
async def bridge_loop(server_ws_url: str, machine_id: str, licence_key: str, tally_url: str):
    uri = f"{server_ws_url}/{machine_id}?key={licence_key}"
    print(f"[Bridge] Connecting to {server_ws_url}…")

    while True:
        try:
            async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as ws:
                print(f"[Bridge] ✓ Connected. Machine ID: {machine_id}")

                # Send hello
                await ws.send(json.dumps({
                    "type":       "hello",
                    "machine_id": machine_id,
                    "tally_url":  tally_url,
                    "version":    "1.0",
                }))

                async for message in ws:
                    try:
                        msg = json.loads(message)
                    except json.JSONDecodeError:
                        continue

                    msg_type = msg.get("type")

                    if msg_type == "ping":
                        await ws.send(json.dumps({"type": "pong"}))

                    elif msg_type == "tally_push":
                        xml     = msg.get("xml", "")
                        req_id  = msg.get("req_id", "")
                        print(f"[Bridge] Received XML push (req_id={req_id}, {len(xml)} chars)")
                        result  = await send_to_tally(xml, tally_url)
                        await ws.send(json.dumps({
                            "type":    "tally_result",
                            "req_id":  req_id,
                            "success": result["success"],
                            "body":    result.get("body", ""),
                            "error":   result.get("error", ""),
                        }))
                        print(f"[Bridge] Result sent: {'✓' if result['success'] else '✗'}")

                    elif msg_type == "tally_status":
                        alive = await ping_tally(tally_url)
                        await ws.send(json.dumps({
                            "type":      "tally_status_result",
                            "connected": alive,
                            "tally_url": tally_url,
                        }))

        except websockets.exceptions.ConnectionClosedError as e:
            print(f"[Bridge] Connection closed: {e}. Reconnecting in {RECONNECT_DELAY}s…")
        except ConnectionRefusedError:
            print(f"[Bridge] Server refused connection. Retrying in {RECONNECT_DELAY}s…")
        except Exception as e:
            print(f"[Bridge] Error: {e}. Retrying in {RECONNECT_DELAY}s…")

        await asyncio.sleep(RECONNECT_DELAY)

# ── WebSocket server endpoint (add to licence_server.py) ─────────────────────
WEBSOCKET_SERVER_CODE = '''
# Add this to licence_server.py for WebSocket support
# pip install websockets

from fastapi import WebSocket, WebSocketDisconnect
import asyncio, json

# Active bridge connections: machine_id → WebSocket
bridge_connections: dict = {}

@app.websocket("/ws/tally/{machine_id}")
async def tally_ws(websocket: WebSocket, machine_id: str, key: str = ""):
    await websocket.accept()
    bridge_connections[machine_id] = websocket
    print(f"Bridge connected: {machine_id}")
    try:
        while True:
            data = await websocket.receive_text()
            msg  = json.loads(data)
            # Forward to any waiting HTTP request handlers
            # (use asyncio.Queue per machine_id in production)
            print(f"Bridge msg from {machine_id}: {msg.get('type')}")
    except WebSocketDisconnect:
        bridge_connections.pop(machine_id, None)
        print(f"Bridge disconnected: {machine_id}")

async def push_to_bridge(machine_id: str, xml: str) -> dict:
    """Call this from /api/tally/push to route through bridge instead of direct HTTP."""
    ws = bridge_connections.get(machine_id)
    if not ws:
        return {"success": False, "error": "Tally Bridge not connected for this machine."}
    req_id = secrets.token_hex(8)
    result_queue = asyncio.Queue()
    # Store queue for response
    pending_requests[req_id] = result_queue
    await ws.send_text(json.dumps({"type": "tally_push", "xml": xml, "req_id": req_id}))
    try:
        result = await asyncio.wait_for(result_queue.get(), timeout=30)
        return result
    except asyncio.TimeoutError:
        return {"success": False, "error": "Tally Bridge did not respond in 30s."}

pending_requests: dict = {}
'''

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Tally Suite Bridge Agent")
    parser.add_argument("--server",    help="WebSocket server URL (wss://yourdomain.com/ws/tally)")
    parser.add_argument("--key",       help="Licence key")
    parser.add_argument("--tally-url", default=TALLY_URL, help="Tally Prime URL (default: http://localhost:9000)")
    parser.add_argument("--setup",     action="store_true", help="Interactive setup")
    args = parser.parse_args()

    cfg = load_config()

    if args.setup or (not cfg.get("server") and not args.server):
        print("=== Tally Suite Bridge Setup ===")
        cfg["server"]      = input("Server WebSocket URL (wss://yourdomain.com/ws/tally): ").strip()
        cfg["licence_key"] = input("Your licence key: ").strip()
        cfg["tally_url"]   = input(f"Tally URL (press Enter for {TALLY_URL}): ").strip() or TALLY_URL
        save_config(cfg)
        print(f"Config saved to {CONFIG_FILE}")

    server      = args.server      or cfg.get("server", "")
    licence_key = args.key         or cfg.get("licence_key", "")
    tally_url   = args.tally_url   or cfg.get("tally_url", TALLY_URL)
    machine_id  = get_machine_id()

    if not server:
        print("ERROR: Server URL not configured. Run with --setup or --server flag.")
        sys.exit(1)

    print(f"=== Tally Suite Bridge v1.0 ===")
    print(f"Machine ID : {machine_id}")
    print(f"Server     : {server}")
    print(f"Tally URL  : {tally_url}")
    print("Press Ctrl+C to stop.\n")

    asyncio.run(bridge_loop(server, machine_id, licence_key, tally_url))

if __name__ == "__main__":
    main()
