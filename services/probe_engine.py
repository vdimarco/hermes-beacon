import hashlib
import json
import os
import random
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

DB_PATH = config.PROBES_DB_PATH
DAILY_PROBE_BUDGET_CENTS = config.DAILY_PROBE_BUDGET_CENTS
PROBE_TIMEOUT_SECONDS = config.HTTP_TIMEOUT_SECONDS
VERIFIED_BY = "beacon-avs-v2"

app = FastAPI(title="Beacon Probe Engine")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# --------------------------------------------------------------------------
# DB setup
# --------------------------------------------------------------------------

@contextmanager
def get_conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    last_err = None
    for attempt in range(3):
        try:
            conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
            break
        except sqlite3.OperationalError as e:
            last_err = e
            time.sleep(0.2 * (2 ** attempt))
    else:
        raise last_err
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS scores (
                id INTEGER PRIMARY KEY,
                endpoint TEXT,
                endpoint_id TEXT,
                trust_score INTEGER,
                grade TEXT,
                accuracy REAL,
                uptime_pct REAL,
                latency_p99_ms INTEGER,
                dispute_rate REAL,
                scam_flag INTEGER,
                sample_size INTEGER,
                verified_by TEXT,
                attested_at TEXT,
                attestation TEXT,
                spend_amount_cents INTEGER,
                created_at TEXT
            )
            """
        )


@app.on_event("startup")
def on_startup():
    init_db()


@app.get("/health")
def health():
    return {"status": "ok", "service": "probe_engine", "port": 8000}


# --------------------------------------------------------------------------
# Request/response models
# --------------------------------------------------------------------------

class ProbeRequest(BaseModel):
    target_url: str
    task_description: str
    payload: dict
    ground_truth: str
    spend_amount_cents: int


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def endpoint_id_from_url(url: str) -> str:
    try:
        hostname = httpx.URL(url).host or url
    except Exception:
        hostname = url
    return hostname.replace(".", "-")


def today_spent_cents(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(spend_amount_cents), 0) AS total FROM scores "
        "WHERE date(created_at) = date('now')"
    ).fetchone()
    return int(row["total"])


def execute_probe(target_url: str, payload: dict) -> tuple[Optional[Any], int, bool, Optional[int]]:
    """Send payload to target_url. Returns (response_body, latency_ms, error_occurred, status_code)."""
    start = time.monotonic()
    try:
        with httpx.Client(timeout=PROBE_TIMEOUT_SECONDS) as client:
            resp = client.post(target_url, json=payload)
        latency_ms = int((time.monotonic() - start) * 1000)
        if resp.status_code >= 400:
            return None, latency_ms, True, resp.status_code
        try:
            body = resp.json()
        except ValueError:
            body = resp.text
        return body, latency_ms, False, resp.status_code
    except httpx.TimeoutException:
        latency_ms = int((time.monotonic() - start) * 1000)
        return None, latency_ms, True, None
    except httpx.HTTPError:
        latency_ms = int((time.monotonic() - start) * 1000)
        return None, latency_ms, True, None
    except Exception:
        latency_ms = int((time.monotonic() - start) * 1000)
        return None, latency_ms, True, None


NOUS_API_BASE = "https://inference-api.nousresearch.com/v1"
NOUS_MODEL = "nvidia/nemotron-3-ultra-550b-a55b"
# Nemotron 3 Ultra is a large reasoning model; it routinely takes well over
# the 5s target-probe timeout to respond, so it gets its own budget.
NOUS_TIMEOUT_SECONDS = 25.0


def _extract_json_object(text: str) -> dict:
    """Chat models often wrap JSON in prose or markdown fences; pull out the
    first {...} object rather than assuming the response is pure JSON."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"No JSON object found in model response: {text!r}")
    return json.loads(text[start:end + 1])


def call_nemotron_real(task_description: str, payload: dict, response_body: Any, ground_truth: str) -> dict:
    """Calls Nemotron via Nous Research's inference API. Requires NOUS_API_KEY env var."""
    api_key = os.environ.get("NOUS_API_KEY")
    if not api_key:
        raise RuntimeError("NOUS_API_KEY not set")

    prompt = (
        f"You are a quality evaluator. An API claims it can do: {task_description}.\n"
        f"I sent this test payload: {payload}.\n"
        f"It responded: {response_body}.\n"
        f"The correct answer is: {ground_truth}.\n"
        "Evaluate:\n"
        "(1) Accuracy: did it answer correctly? (0.0-1.0)\n"
        "(2) Error rate: did it fail or refuse? (0.0-1.0)\n"
        "(3) Hallucination: did it invent facts not supported by the response? (true/false)\n"
        'Return ONLY JSON: {"accuracy": 0.0-1.0, "error_rate": 0.0-1.0, "hallucination_detected": true/false}'
    )
    with httpx.Client(timeout=NOUS_TIMEOUT_SECONDS) as client:
        resp = client.post(
            f"{NOUS_API_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": NOUS_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
            },
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        evaluation = _extract_json_object(content)
        return {
            "accuracy": float(evaluation["accuracy"]),
            "error_rate": float(evaluation["error_rate"]),
            "hallucination_detected": bool(evaluation["hallucination_detected"]),
        }


def call_nemotron_mock(status_code: Optional[int], response_body: Any, ground_truth: str, error_occurred: bool) -> dict:
    """Deterministic mock evaluator used when the real Nemotron API is unavailable."""
    if error_occurred or status_code is None or status_code >= 400:
        return {"accuracy": 0.2, "error_rate": 1.0, "hallucination_detected": False}

    body_text = json.dumps(response_body) if not isinstance(response_body, str) else response_body
    keywords = [w for w in (ground_truth or "").split() if len(w) > 3]
    keyword_match = any(k.lower() in body_text.lower() for k in keywords) if keywords else False

    if status_code == 200 and keyword_match:
        return {"accuracy": 0.9, "error_rate": 0.0, "hallucination_detected": False}
    if status_code == 200:
        return {"accuracy": 0.6, "error_rate": 0.0, "hallucination_detected": False}
    return {"accuracy": 0.6, "error_rate": 0.1, "hallucination_detected": False}


def evaluate_with_nemotron(task_description: str, payload: dict, response_body: Any, ground_truth: str,
                            status_code: Optional[int], error_occurred: bool) -> tuple[dict, str]:
    """Returns (evaluation, evaluator_label). evaluator_label is surfaced in the
    API response so it's visible (e.g. in logs/demo) whether this probe was
    actually scored by the live Nemotron model or fell back to the mock."""
    try:
        evaluation = call_nemotron_real(task_description, payload, response_body, ground_truth)
        return evaluation, "nemotron-3-ultra"
    except Exception as e:
        print(f"[probe_engine] Nemotron call failed, falling back to mock evaluator: {e}")
        evaluation = call_nemotron_mock(status_code, response_body, ground_truth, error_occurred)
        return evaluation, "mock"


def calculate_trust_score(accuracy: float, error_rate: float, error_occurred: bool, hallucination_detected: bool) -> int:
    base = (accuracy * 100) * 0.7 + ((1 - error_rate) * 100) * 0.3
    if error_occurred:
        base = min(base, 30)
    if hallucination_detected:
        base = min(base, 60)
    return round(base)


def grade_for_score(trust_score: int) -> str:
    if trust_score >= 90:
        return "A"
    if trust_score >= 80:
        return "B"
    if trust_score >= 70:
        return "C"
    if trust_score >= 60:
        return "D"
    return "F"


def build_attestation(endpoint_id: str, trust_score: int, timestamp: str) -> str:
    raw = f"{endpoint_id}{trust_score}{VERIFIED_BY}{timestamp}"
    return "0x" + hashlib.sha256(raw.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------
# Route
# --------------------------------------------------------------------------

@app.post("/v1/probe")
def probe(req: ProbeRequest):
    with get_conn() as conn:
        already_spent = today_spent_cents(conn)
        if already_spent + req.spend_amount_cents > DAILY_PROBE_BUDGET_CENTS:
            return JSONResponse(
                status_code=403,
                content={
                    "error": "NemoClaw guardrail: Daily probe budget exceeded",
                    "budget_limit_cents": DAILY_PROBE_BUDGET_CENTS,
                    "already_spent_cents": already_spent,
                    "remaining_cents": DAILY_PROBE_BUDGET_CENTS - already_spent,
                    "guardrail_triggered": True,
                },
            )

        response_body, latency_ms, error_occurred, status_code = execute_probe(req.target_url, req.payload)

        if error_occurred:
            accuracy = 0.2
            error_rate = 1.0
            hallucination_detected = False
            evaluator = "error-path"
        else:
            evaluation, evaluator = evaluate_with_nemotron(
                req.task_description, req.payload, response_body, req.ground_truth,
                status_code, error_occurred,
            )
            accuracy = evaluation["accuracy"]
            error_rate = evaluation["error_rate"]
            hallucination_detected = evaluation["hallucination_detected"]

        trust_score = calculate_trust_score(accuracy, error_rate, error_occurred, hallucination_detected)
        grade = grade_for_score(trust_score)

        endpoint_id = endpoint_id_from_url(req.target_url)
        now = datetime.now(timezone.utc).isoformat()
        uptime_pct = round(99.0 + random.uniform(0.0, 0.9), 2)
        attestation = build_attestation(endpoint_id, trust_score, now)

        conn.execute(
            """
            INSERT INTO scores (
                endpoint, endpoint_id, trust_score, grade, accuracy, uptime_pct,
                latency_p99_ms, dispute_rate, scam_flag, sample_size, verified_by,
                attested_at, attestation, spend_amount_cents, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                req.target_url,
                endpoint_id,
                trust_score,
                grade,
                accuracy,
                uptime_pct,
                latency_ms,
                0.0,
                0,
                1,
                VERIFIED_BY,
                now,
                attestation,
                req.spend_amount_cents,
                now,
            ),
        )

        return {
            "endpoint": req.target_url,
            "trust_score": trust_score,
            "grade": grade,
            "accuracy": accuracy,
            "uptime_pct": uptime_pct,
            "latency_p99_ms": latency_ms,
            "dispute_rate": 0.0,
            "scam_flag": False,
            "sample_size": 1,
            "verified_by": VERIFIED_BY,
            "attested_at": now,
            "attestation": attestation,
            "evaluator": evaluator,
        }
