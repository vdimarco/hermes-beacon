"""Create db/ and initialize all 4 SQLite databases, then seed them. Idempotent."""
import os
import subprocess
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "services"))

import config


def main():
    os.makedirs(config.DATABASE_DIR, exist_ok=True)

    import probe_engine
    import attestation
    import escrow_gate

    probe_engine.init_db()
    attestation.init_db()
    escrow_gate.init_db()
    print("Initialized: probes.db, attestations.db, disputes.db, escrow.db")

    subprocess.run([sys.executable, os.path.join(BASE_DIR, "scripts", "seed_data.py")], check=True)
    subprocess.run([sys.executable, os.path.join(BASE_DIR, "scripts", "seed_attestations.py")], check=True)


if __name__ == "__main__":
    main()
