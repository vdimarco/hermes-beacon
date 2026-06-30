"""Insert 3 synthetic scores into probes.db for local testing.

These endpoints (api.weather-ai.com etc.) are fictional -- they don't
resolve to real services. Rows are marked synthetic=1 so the ledger API
and demo UI can flag them as sample data rather than a real probe
result."""
import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "services"))

from probe_engine import DB_PATH, VERIFIED_BY, build_attestation, init_db

SEED_ROWS = [
    {
        "endpoint": "https://api.weather-ai.com/v1/forecast",
        "trust_score": 94,
        "grade": "A",
        "accuracy": 0.97,
        "uptime_pct": 99.91,
        "latency_p99_ms": 180,
        "dispute_rate": 0.001,
        "scam_flag": 0,
        "sample_size": 142830,
        "spend_amount_cents": 3,
    },
    {
        "endpoint": "https://api.tradebot-x.io/v2/quote",
        "trust_score": 62,
        "grade": "D",
        "accuracy": 0.69,
        "uptime_pct": 96.84,
        "latency_p99_ms": 940,
        "dispute_rate": 0.046,
        "scam_flag": 0,
        "sample_size": 5310,
        "spend_amount_cents": 5,
    },
    {
        "endpoint": "https://api.scamcoin-signals.net/v1/predict",
        "trust_score": 43,
        "grade": "F",
        "accuracy": 0.31,
        "uptime_pct": 88.05,
        "latency_p99_ms": 2640,
        "dispute_rate": 0.21,
        "scam_flag": 1,
        "sample_size": 1208,
        "spend_amount_cents": 2,
    },
]


def seed():
    init_db()
    conn = sqlite3.connect(DB_PATH)
    existing = conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0]
    if existing > 0:
        conn.close()
        print(f"Skipped seeding: {DB_PATH} already has {existing} row(s)")
        return

    now = datetime.now(timezone.utc).isoformat()
    for row in SEED_ROWS:
        endpoint_id = row["endpoint"].split("//")[1].split("/")[0].replace(".", "-")
        attestation = build_attestation(endpoint_id, row["trust_score"], now)
        conn.execute(
            """
            INSERT INTO scores (
                endpoint, endpoint_id, trust_score, grade, accuracy, uptime_pct,
                latency_p99_ms, dispute_rate, scam_flag, sample_size, verified_by,
                attested_at, attestation, spend_amount_cents, created_at, synthetic
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["endpoint"],
                endpoint_id,
                row["trust_score"],
                row["grade"],
                row["accuracy"],
                row["uptime_pct"],
                row["latency_p99_ms"],
                row["dispute_rate"],
                row["scam_flag"],
                row["sample_size"],
                VERIFIED_BY,
                now,
                attestation,
                row["spend_amount_cents"],
                now,
                1,
            ),
        )
    conn.commit()
    conn.close()
    print(f"Seeded {len(SEED_ROWS)} rows into {DB_PATH}")


if __name__ == "__main__":
    seed()
