"""Escrow Gate: mocks the Enforcement Plane + Stripe conditional escrow.

Calls the Ledger API for a trust score, decides whether a payment may
proceed, and (if so) collects Beacon's 0.5% escrow fee. Decisions are
logged to escrow.db.
"""
import os
import sqlite3
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

DB_PATH = config.ESCROW_DB_PATH
LEDGER_API_URL = config.LEDGER_API_URL
TRUST_THRESHOLD = 70
ESCROW_FEE_RATE = 0.005

app = FastAPI(title="Beacon Escrow Gate")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/health")
def health():
    return {"status": "ok", "service": "escrow_gate", "port": 8003}


@contextmanager
def get_conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS escrow_events (
                id TEXT PRIMARY KEY,
                endpoint_id TEXT NOT NULL,
                payment_amount_cents INTEGER NOT NULL,
                trust_score INTEGER,
                decision TEXT NOT NULL,
                fee_cents INTEGER,
                net_amount_cents INTEGER,
                reason TEXT,
                timestamp TEXT NOT NULL
            )
            """
        )
        conn.commit()


@app.on_event("startup")
def on_startup():
    init_db()


class ValidateRequest(BaseModel):
    endpoint_id: str
    payment_amount_cents: int


class ExecuteRequest(BaseModel):
    escrow_id: str
    stripe_payment_intent_id: str


@app.post("/v1/escrow/validate")
def validate_escrow(req: ValidateRequest):
    try:
        with httpx.Client(timeout=config.HTTP_TIMEOUT_SECONDS) as client:
            resp = client.get(f"{LEDGER_API_URL}/v1/score/{req.endpoint_id}")
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Ledger API unreachable: {e}")

    if resp.status_code == 404:
        raise HTTPException(
            status_code=404,
            detail=f"No score found for endpoint_id '{req.endpoint_id}'",
        )
    resp.raise_for_status()
    score = resp.json()

    trust_score = score["trust_score"]
    grade = score["grade"]
    escrow_id = str(uuid.uuid4())
    timestamp = datetime.now(timezone.utc).isoformat()

    if trust_score >= TRUST_THRESHOLD:
        fee_cents = int(req.payment_amount_cents * ESCROW_FEE_RATE)
        net_amount_cents = req.payment_amount_cents - fee_cents
        decision = "PASS"
        reason = (
            f"Beacon trust_score: {trust_score} — PASS. "
            f"Escrow fee: ${fee_cents / 100:.2f}."
        )
        can_pay = True
    else:
        fee_cents = None
        net_amount_cents = None
        decision = "BLOCK"
        reason = (
            f"Beacon trust_score: {trust_score} — BLOCK. "
            f"{grade} grade. Endpoint failed verification."
        )
        can_pay = False

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO escrow_events
                (id, endpoint_id, payment_amount_cents, trust_score, decision,
                 fee_cents, net_amount_cents, reason, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                escrow_id,
                req.endpoint_id,
                req.payment_amount_cents,
                trust_score,
                decision,
                fee_cents,
                net_amount_cents,
                reason,
                timestamp,
            ),
        )
        conn.commit()

    return {
        "escrow_id": escrow_id,
        "can_pay": can_pay,
        "decision": decision,
        "fee_cents": fee_cents,
        "net_amount_cents": net_amount_cents,
        "reason": reason,
        "trust_score": trust_score,
    }


@app.post("/v1/escrow/execute")
def execute_escrow(req: ExecuteRequest):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM escrow_events WHERE id = ?", (req.escrow_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"No escrow found for id '{req.escrow_id}'")
        if row["decision"] != "PASS":
            raise HTTPException(status_code=400, detail="Escrow was not approved for payment")

    return {
        "status": "released",
        "fee_cents": row["fee_cents"],
        "net_amount_cents": row["net_amount_cents"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/v1/escrow/{escrow_id}")
def get_escrow(escrow_id: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM escrow_events WHERE id = ?", (escrow_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail=f"No escrow found for id '{escrow_id}'")
        return {
            "escrow_id": row["id"],
            "endpoint_id": row["endpoint_id"],
            "payment_amount_cents": row["payment_amount_cents"],
            "trust_score": row["trust_score"],
            "decision": row["decision"],
            "fee_cents": row["fee_cents"],
            "net_amount_cents": row["net_amount_cents"],
            "reason": row["reason"],
            "timestamp": row["timestamp"],
        }
