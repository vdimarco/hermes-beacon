"""Read-only Ledger API: serves trust scores from probes.db (written by probe_engine.py)."""
import os
import sqlite3
import sys
from contextlib import contextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

DB_PATH = config.PROBES_DB_PATH

app = FastAPI(title="Beacon Ledger API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/health")
def health():
    return {"status": "ok", "service": "ledger_api", "port": 8001}


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def escrow_recommendation(trust_score: int) -> str:
    if trust_score >= 70:
        return "PASS"
    if trust_score >= 50:
        return "HOLD"
    return "BLOCK"


def row_to_score(row: sqlite3.Row) -> dict:
    return {
        "endpoint": row["endpoint"],
        "trust_score": row["trust_score"],
        "grade": row["grade"],
        "accuracy": row["accuracy"],
        "uptime_pct": row["uptime_pct"],
        "latency_p99_ms": row["latency_p99_ms"],
        "dispute_rate": row["dispute_rate"],
        "scam_flag": bool(row["scam_flag"]),
        "sample_size": row["sample_size"],
        "verified_by": row["verified_by"],
        "attested_at": row["attested_at"],
        "attestation": row["attestation"],
        "escrow_recommendation": escrow_recommendation(row["trust_score"]),
    }


def fetch_latest_score(conn: sqlite3.Connection, endpoint_id: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM scores
        WHERE endpoint_id = ?
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (endpoint_id,),
    ).fetchone()


class BatchRequest(BaseModel):
    endpoint_ids: list[str]


@app.get("/v1/score/{endpoint_id}")
def get_score(endpoint_id: str):
    with get_conn() as conn:
        row = fetch_latest_score(conn, endpoint_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"No score found for endpoint_id '{endpoint_id}'")
        return row_to_score(row)


@app.get("/v1/scores")
def list_scores():
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT s.* FROM scores s
            INNER JOIN (
                SELECT endpoint_id, MAX(created_at) AS max_created_at
                FROM scores
                GROUP BY endpoint_id
            ) latest
            ON s.endpoint_id = latest.endpoint_id AND s.created_at = latest.max_created_at
            ORDER BY s.trust_score DESC
            """
        ).fetchall()
        return [row_to_score(row) for row in rows]


@app.post("/v1/score/batch")
def batch_scores(req: BatchRequest):
    with get_conn() as conn:
        results = []
        for endpoint_id in req.endpoint_ids:
            row = fetch_latest_score(conn, endpoint_id)
            if row is not None:
                results.append(row_to_score(row))
        return results
