"""Escrow Gate: the Enforcement Plane gating payment on a Beacon trust score.

Calls the Ledger API for a trust score and decides whether a payment may
proceed. On PASS, it creates and confirms a real Stripe PaymentIntent
(test mode) for the payment amount, holding funds with manual capture
until /v1/escrow/execute releases them. On BLOCK, no Stripe call is made
at all — that's the actual safety guarantee: a bad endpoint never gets a
payment intent, let alone captured funds. Decisions are logged to
escrow.db.
"""
import os
import sqlite3
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

import httpx
import stripe
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from posthog_client import posthog_client

DB_PATH = config.ESCROW_DB_PATH
LEDGER_API_URL = config.LEDGER_API_URL
TRUST_THRESHOLD = 70
ESCROW_FEE_RATE = 0.005

# Stripe's built-in test-mode payment method — confirms a PaymentIntent
# without collecting real card details. Test mode only.
STRIPE_TEST_PAYMENT_METHOD = "pm_card_visa"

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")

app = FastAPI(title="Beacon Escrow Gate")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/health")
def health():
    return {"status": "ok", "service": "escrow_gate", "port": 8003, "stripe_configured": bool(stripe.api_key)}


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
        existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(escrow_events)")}
        for col in ("stripe_payment_intent_id", "stripe_status"):
            if col not in existing_cols:
                conn.execute(f"ALTER TABLE escrow_events ADD COLUMN {col} TEXT")
        conn.commit()


@app.on_event("startup")
def on_startup():
    init_db()


class ValidateRequest(BaseModel):
    endpoint_id: str
    payment_amount_cents: int


class ExecuteRequest(BaseModel):
    escrow_id: str
    stripe_payment_intent_id: str | None = None


def create_stripe_hold(payment_amount_cents: int, endpoint_id: str, escrow_id: str) -> tuple[str | None, str | None]:
    """Creates and confirms a Stripe PaymentIntent with manual capture, i.e.
    a real (test-mode) hold on funds. Returns (payment_intent_id, status),
    or (None, None) if Stripe isn't configured or the call fails — escrow
    still proceeds, just without a live Stripe-backed hold."""
    if not stripe.api_key:
        return None, None
    try:
        intent = stripe.PaymentIntent.create(
            amount=payment_amount_cents,
            currency="usd",
            payment_method_types=["card"],
            payment_method=STRIPE_TEST_PAYMENT_METHOD,
            capture_method="manual",
            confirm=True,
            metadata={"endpoint_id": endpoint_id, "escrow_id": escrow_id, "source": "beacon-escrow-gate"},
        )
        return intent.id, intent.status
    except stripe.error.StripeError as e:
        print(f"[escrow_gate] Stripe PaymentIntent creation failed: {e}")
        return None, None


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

    stripe_payment_intent_id = None
    stripe_status = None

    if trust_score >= TRUST_THRESHOLD:
        fee_cents = int(req.payment_amount_cents * ESCROW_FEE_RATE)
        net_amount_cents = req.payment_amount_cents - fee_cents
        decision = "PASS"
        can_pay = True

        # The actual safety mechanism: a Stripe hold is only ever created
        # for a PASS decision. A BLOCKed endpoint never gets this far.
        stripe_payment_intent_id, stripe_status = create_stripe_hold(
            req.payment_amount_cents, req.endpoint_id, escrow_id
        )

        if stripe_payment_intent_id:
            reason = (
                f"Beacon trust_score: {trust_score} — PASS. "
                f"Escrow fee: ${fee_cents / 100:.2f}. "
                f"Stripe hold {stripe_payment_intent_id} ({stripe_status})."
            )
        else:
            reason = (
                f"Beacon trust_score: {trust_score} — PASS. "
                f"Escrow fee: ${fee_cents / 100:.2f}."
            )
    else:
        fee_cents = None
        net_amount_cents = None
        decision = "BLOCK"
        reason = (
            f"Beacon trust_score: {trust_score} — BLOCK. "
            f"{grade} grade. Endpoint failed verification. No Stripe charge attempted."
        )
        can_pay = False

    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO escrow_events
                (id, endpoint_id, payment_amount_cents, trust_score, decision,
                 fee_cents, net_amount_cents, reason, timestamp,
                 stripe_payment_intent_id, stripe_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                stripe_payment_intent_id,
                stripe_status,
            ),
        )
        conn.commit()

    if posthog_client:
        posthog_client.capture(
            req.endpoint_id,
            "escrow_validated",
            properties={
                "endpoint_id": req.endpoint_id,
                "decision": decision,
                "trust_score": trust_score,
                "payment_amount_cents": req.payment_amount_cents,
                "stripe_backed": stripe_payment_intent_id is not None,
            },
        )

    return {
        "escrow_id": escrow_id,
        "can_pay": can_pay,
        "decision": decision,
        "fee_cents": fee_cents,
        "net_amount_cents": net_amount_cents,
        "reason": reason,
        "trust_score": trust_score,
        "stripe_payment_intent_id": stripe_payment_intent_id,
        "stripe_status": stripe_status,
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

    intent_id = row["stripe_payment_intent_id"]
    if intent_id and stripe.api_key:
        try:
            captured = stripe.PaymentIntent.capture(intent_id)
            with get_conn() as conn:
                conn.execute(
                    "UPDATE escrow_events SET stripe_status = ? WHERE id = ?",
                    (captured.status, req.escrow_id),
                )
                conn.commit()
            if posthog_client:
                posthog_client.capture(
                    row["endpoint_id"],
                    "escrow_executed",
                    properties={
                        "endpoint_id": row["endpoint_id"],
                        "escrow_id": req.escrow_id,
                        "fee_cents": row["fee_cents"],
                        "net_amount_cents": row["net_amount_cents"],
                        "stripe_backed": True,
                    },
                )
            return {
                "status": "released" if captured.status == "succeeded" else captured.status,
                "fee_cents": row["fee_cents"],
                "net_amount_cents": row["net_amount_cents"],
                "stripe_payment_intent_id": intent_id,
                "stripe_status": captured.status,
                "stripe_amount_received_cents": captured.amount_received,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        except stripe.error.StripeError as e:
            raise HTTPException(status_code=502, detail=f"Stripe capture failed: {e}")

    if posthog_client:
        posthog_client.capture(
            row["endpoint_id"],
            "escrow_executed",
            properties={
                "endpoint_id": row["endpoint_id"],
                "escrow_id": req.escrow_id,
                "fee_cents": row["fee_cents"],
                "net_amount_cents": row["net_amount_cents"],
                "stripe_backed": False,
            },
        )

    return {
        "status": "released",
        "fee_cents": row["fee_cents"],
        "net_amount_cents": row["net_amount_cents"],
        "stripe_payment_intent_id": intent_id,
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
            "stripe_payment_intent_id": row["stripe_payment_intent_id"],
            "stripe_status": row["stripe_status"],
        }
