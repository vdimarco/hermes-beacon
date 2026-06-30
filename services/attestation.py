"""Verification Plane (EigenLayer AVS) mock: issues and serves attestations, logs disputes."""
import hashlib
import os
import sqlite3
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

ATTESTATIONS_DB_PATH = config.ATTESTATIONS_DB_PATH
DISPUTES_DB_PATH = config.DISPUTES_DB_PATH

VALIDATORS = {"beacon-avs-v2", "beacon-node-1", "beacon-node-2"}

app = FastAPI(title="Beacon Verification Plane")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/health")
def health():
    return {"status": "ok", "service": "attestation", "port": 8002}


@contextmanager
def get_conn(db_path: str):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn(ATTESTATIONS_DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS attestations (
                id INTEGER PRIMARY KEY,
                endpoint_id TEXT,
                trust_score INTEGER,
                validator_id TEXT,
                attestation TEXT,
                timestamp TEXT
            )
            """
        )
    with get_conn(DISPUTES_DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS disputes (
                id INTEGER PRIMARY KEY,
                dispute_id TEXT,
                endpoint_id TEXT,
                reason TEXT,
                timestamp TEXT
            )
            """
        )


@app.on_event("startup")
def on_startup():
    init_db()


class AttestRequest(BaseModel):
    endpoint_id: str
    trust_score: int
    validator_id: str


class DisputeRequest(BaseModel):
    endpoint_id: str
    reason: str


def build_attestation(endpoint_id: str, trust_score: int, validator_id: str, timestamp: str) -> str:
    raw = f"{endpoint_id}{trust_score}{validator_id}{timestamp}"
    return "0x" + hashlib.sha256(raw.encode()).hexdigest()[:16]


def row_to_attestation(row: sqlite3.Row) -> dict:
    return {
        "endpoint_id": row["endpoint_id"],
        "trust_score": row["trust_score"],
        "validator_id": row["validator_id"],
        "verified_by": row["validator_id"],
        "attestation": row["attestation"],
        "attested_at": row["timestamp"],
    }


@app.post("/v1/attest")
def attest(req: AttestRequest):
    if req.validator_id not in VALIDATORS:
        raise HTTPException(status_code=400, detail=f"Unknown validator_id '{req.validator_id}'")

    now = datetime.now(timezone.utc).isoformat()
    attestation = build_attestation(req.endpoint_id, req.trust_score, req.validator_id, now)

    with get_conn(ATTESTATIONS_DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO attestations (endpoint_id, trust_score, validator_id, attestation, timestamp)
            VALUES (?, ?, ?, ?, ?)
            """,
            (req.endpoint_id, req.trust_score, req.validator_id, attestation, now),
        )

    return {
        "endpoint_id": req.endpoint_id,
        "trust_score": req.trust_score,
        "validator_id": req.validator_id,
        "verified_by": req.validator_id,
        "attestation": attestation,
        "attested_at": now,
    }


@app.get("/v1/verify/{attestation}")
def verify(attestation: str):
    with get_conn(ATTESTATIONS_DB_PATH) as conn:
        row = conn.execute(
            "SELECT * FROM attestations WHERE attestation = ? ORDER BY id DESC LIMIT 1",
            (attestation,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"No attestation found for '{attestation}'")
        return row_to_attestation(row)


@app.post("/v1/dispute")
def dispute(req: DisputeRequest):
    dispute_id = "0x" + uuid.uuid4().hex[:16]
    now = datetime.now(timezone.utc).isoformat()

    with get_conn(DISPUTES_DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO disputes (dispute_id, endpoint_id, reason, timestamp)
            VALUES (?, ?, ?, ?)
            """,
            (dispute_id, req.endpoint_id, req.reason, now),
        )

    return {"reprobe_scheduled": True, "dispute_id": dispute_id, "timestamp": now}
