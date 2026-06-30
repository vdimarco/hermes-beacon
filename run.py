"""Master launcher: starts probe_engine, ledger_api, attestation, escrow_gate
on 127.0.0.1 (internal-only), and the public gateway (static site + /api/*
reverse proxy) on 0.0.0.0:$PORT. One command brings up the whole stack,
locally or in a container.
"""
import os
import signal
import subprocess
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
import config

INTERNAL_SERVICES = [
    ("services.probe_engine:app", 8000),
    ("services.ledger_api:app", 8001),
    ("services.attestation:app", 8002),
    ("services.escrow_gate:app", 8003),
]

processes = []


def start_services():
    env = os.environ.copy()
    env["PYTHONPATH"] = BASE_DIR + os.pathsep + env.get("PYTHONPATH", "")

    for module, port in INTERNAL_SERVICES:
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", module, "--host", "127.0.0.1", "--port", str(port)],
            cwd=BASE_DIR,
            env=env,
        )
        processes.append(proc)
        print(f"Started {module} on 127.0.0.1:{port} (pid {proc.pid})")

    gateway_proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "services.gateway:app", "--host", "0.0.0.0", "--port", str(config.GATEWAY_PORT)],
        cwd=BASE_DIR,
        env=env,
    )
    processes.append(gateway_proc)
    print(f"Started gateway on 0.0.0.0:{config.GATEWAY_PORT} (pid {gateway_proc.pid})")


def shutdown(*_args):
    print("\nShutting down all services...")
    for proc in processes:
        if proc.poll() is None:
            proc.terminate()
    for proc in processes:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    sys.exit(0)


def main():
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    os.makedirs(config.DATABASE_DIR, exist_ok=True)
    start_services()
    print("\nBeacon stack is up:")
    print(f"  http://localhost:{config.GATEWAY_PORT}  gateway (open this in your browser)")
    print("  probe_engine, ledger_api, attestation, escrow_gate are internal-only (127.0.0.1)")
    print("\nPress Ctrl+C to stop all services.\n")

    while True:
        time.sleep(1)
        for proc in processes:
            if proc.poll() is not None:
                print(f"Service pid {proc.pid} exited unexpectedly (code {proc.returncode}).")
                shutdown()


if __name__ == "__main__":
    main()
