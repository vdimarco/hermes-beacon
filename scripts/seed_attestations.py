"""Insert 3 synthetic attestations into attestations.db for local testing."""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "services"))

from attestation import VALIDATORS, build_attestation, get_conn, init_db, ATTESTATIONS_DB_PATH

SEED_ROWS = [
    {"endpoint": "https://api.weather-ai.com/v1/forecast", "trust_score": 94, "validator_id": "beacon-avs-v2"},
    {"endpoint": "https://api.tradebot-x.io/v2/quote", "trust_score": 62, "validator_id": "beacon-node-1"},
    {"endpoint": "https://api.scamcoin-signals.net/v1/predict", "trust_score": 43, "validator_id": "beacon-node-2"},
]


def seed():
    init_db()
    with get_conn(ATTESTATIONS_DB_PATH) as conn:
        existing = conn.execute("SELECT COUNT(*) FROM attestations").fetchone()[0]
        if existing > 0:
            print(f"Skipped seeding: {ATTESTATIONS_DB_PATH} already has {existing} row(s)")
            return

        now = datetime.now(timezone.utc).isoformat()
        for row in SEED_ROWS:
            endpoint_id = row["endpoint"].split("//")[1].split("/")[0].replace(".", "-")
            assert row["validator_id"] in VALIDATORS
            attestation = build_attestation(endpoint_id, row["trust_score"], row["validator_id"], now)
            conn.execute(
                """
                INSERT INTO attestations (endpoint_id, trust_score, validator_id, attestation, timestamp)
                VALUES (?, ?, ?, ?, ?)
                """,
                (endpoint_id, row["trust_score"], row["validator_id"], attestation, now),
            )
    print(f"Seeded {len(SEED_ROWS)} rows into {ATTESTATIONS_DB_PATH}")


if __name__ == "__main__":
    seed()
