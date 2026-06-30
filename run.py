"""Master launcher: starts probe_engine, ledger_api, attestation, escrow_gate,
and a static file server for the demo HTML, all in one command.
"""
import os
import signal
import subprocess
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

SERVICES = [
    ("services.probe_engine:app", 8000),
    ("services.ledger_api:app", 8001),
    ("services.attestation:app", 8002),
    ("services.escrow_gate:app", 8003),
]

processes = []


def start_services():
    env = os.environ.copy()
    env["PYTHONPATH"] = BASE_DIR + os.pathsep + env.get("PYTHONPATH", "")

    for module, port in SERVICES:
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", module, "--host", "0.0.0.0", "--port", str(port)],
            cwd=BASE_DIR,
            env=env,
        )
        processes.append(proc)
        print(f"Started {module} on port {port} (pid {proc.pid})")

    static_proc = subprocess.Popen(
        [sys.executable, "-m", "http.server", "8080", "--directory", os.path.join(BASE_DIR, "static")],
        cwd=BASE_DIR,
        env=env,
    )
    processes.append(static_proc)
    print(f"Started static demo server on port 8080 (pid {static_proc.pid})")


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

    start_services()
    print("\nBeacon stack is up:")
    print("  http://localhost:8000  probe_engine")
    print("  http://localhost:8001  ledger_api")
    print("  http://localhost:8002  attestation")
    print("  http://localhost:8003  escrow_gate")
    print("  http://localhost:8080  demo (open this in your browser)")
    print("\nPress Ctrl+C to stop all services.\n")

    while True:
        time.sleep(1)
        for proc in processes:
            if proc.poll() is not None:
                print(f"Service pid {proc.pid} exited unexpectedly (code {proc.returncode}).")
                shutdown()


if __name__ == "__main__":
    main()
