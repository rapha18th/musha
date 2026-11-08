"""Musha's WhatsApp line on this computer: the API, with the WhatsApp simulator playing Meta.

    backend/.venv/Scripts/python whatsapp-sim/run.py        (macOS/Linux: backend/.venv/bin/python)

It starts the simulator on port 8090 and the API on port 8000. Both take the same settings, fresh each run:
the four WHATSAPP_* settings with a fictional token, phone number ID, app secret and verify token,
WHATSAPP_GRAPH_BASE pointed at the simulator, and MUSHA_MANAGER_PHONE as the simulated phone's number. The
API keeps its shops in a database of its own, backend/musha-sim.db, so the usual one stays as it was. Once
both answer, the simulator makes Meta's webhook check against the API, once. The rest of .env applies as
usual: PayPal, Gemini and the sandbox sign-ins.

Open the phone at http://127.0.0.1:8090 and the dashboards at http://localhost:5174 (npm run dev in
frontend/) or http://localhost:8000 (after npm run build). Ctrl+C stops both.
"""
import argparse
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent / "backend"
NUMBER_ID = "115550168742001"  # the simulated test number's phone number ID
PHONE = "+263771234567"  # the manager's phone in the simulator


def free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def wait_for(url: str, process: subprocess.Popen, name: str, seconds: float = 60) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if process.poll() is not None:
            raise SystemExit(f"The {name} stopped as it started. Its log is above.")
        try:
            if httpx.get(url, timeout=2).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.4)
    raise SystemExit(f"The {name} gave no answer at {url} in {seconds:.0f} seconds.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Musha with the WhatsApp simulator as Meta.")
    parser.add_argument("--api-port", type=int, default=8000)
    parser.add_argument("--sim-port", type=int, default=8090)
    parser.add_argument("--phone", default=PHONE, help="the manager's number on the simulated phone, E.164")
    parser.add_argument("--database", default=f"sqlite:///{(BACKEND / 'musha-sim.db').as_posix()}",
                        help="the API's database; the default keeps the usual backend/musha.db as it was")
    parser.add_argument("--pace", type=float, default=0.6, help="seconds between Musha's send and each status")
    args = parser.parse_args()

    for port in (args.api_port, args.sim_port):
        if not free(port):
            raise SystemExit(f"Port {port} is taken. Stop what runs there, or pass --api-port or --sim-port.")

    graph = f"http://127.0.0.1:{args.sim_port}"
    api = f"http://127.0.0.1:{args.api_port}"
    env = {
        **os.environ,
        "WHATSAPP_TOKEN": "sim-" + secrets.token_urlsafe(24),
        "WHATSAPP_PHONE_NUMBER_ID": NUMBER_ID,
        "WHATSAPP_APP_SECRET": secrets.token_hex(16),
        "WHATSAPP_VERIFY_TOKEN": secrets.token_urlsafe(16),
        "WHATSAPP_GRAPH_BASE": graph,
        "MUSHA_MANAGER_PHONE": args.phone,
        "MUSHA_WEBHOOK": f"{api}/api/whatsapp/webhook",
        "SIM_PACE": str(args.pace),
        "DATABASE_URL": args.database,
        "PYTHONUNBUFFERED": "1",
    }
    python = sys.executable
    sim = subprocess.Popen([python, "-m", "uvicorn", "--factory", "sim:create_app", "--app-dir", str(HERE),
                            "--host", "127.0.0.1", "--port", str(args.sim_port), "--log-level", "warning"], env=env)
    server = subprocess.Popen([python, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(args.api_port)],
                              cwd=BACKEND, env=env)
    try:
        wait_for(f"{graph}/sim/health", sim, "simulator")
        wait_for(f"{api}/api/config", server, "API", seconds=120)
        check = httpx.post(f"{graph}/sim/verify", timeout=30).json()
        if not check.get("ok"):
            raise SystemExit(f"Meta's webhook check failed: the API answered {check.get('status')}.")
        print(f"\nMeta's webhook check passed: the API answered the challenge for the verify token.\n"
              f"  The phone      {graph}\n"
              f"  The API        {api}\n"
              f"  The webhook    {api}/api/whatsapp/webhook\n"
              f"  The Graph API  {graph}\n"
              f"Ctrl+C stops both.\n", flush=True)
        while sim.poll() is None and server.poll() is None:
            time.sleep(0.5)
        raise SystemExit("One of the two stopped. Its log is above.")
    except KeyboardInterrupt:
        pass
    finally:
        for p in (sim, server):
            if p.poll() is None:
                p.terminate()
        for p in (sim, server):
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    main()
