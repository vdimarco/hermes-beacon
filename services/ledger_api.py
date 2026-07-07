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
import reputation
from posthog_client import posthog_client

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
    return reputation.escrow_recommendation(trust_score)


def summarize_endpoint(rows: list[sqlite3.Row]) -> dict:
    """Turn all of an endpoint's probe rows into one score object. The
    reputation index/grade/scam verdict are aggregated over the whole history
    (see reputation.compute_index); the newest row supplies display metadata
    (endpoint URL, latency, uptime, evaluator, latest status)."""
    latest = rows[0]  # rows arrive newest-first
    rep = reputation.compute_index(rows)
    result = row_to_score(latest)
    result["trust_score"] = rep["index"]
    result["grade"] = rep["grade"]
    result["scam_flag"] = rep["scam"] or bool(latest["scam_flag"])
    result["sample_size"] = rep["sample_calls"]
    result["breakdown"] = rep["breakdown"]
    result["escrow_recommendation"] = escrow_recommendation(rep["index"])
    return result


def row_to_score(row: sqlite3.Row) -> dict:
    columns = row.keys()
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
        "evaluator": row["evaluator"] if "evaluator" in columns else None,
        "synthetic": bool(row["synthetic"]) if "synthetic" in columns else False,
        "status": row["request_status"] if "request_status" in columns else None,
    }


def fetch_endpoint_rows(conn: sqlite3.Connection, endpoint_id: str) -> list[sqlite3.Row]:
    """All probe rows for one endpoint, newest first (the reputation index
    aggregates the full history, not just the latest probe)."""
    return conn.execute(
        """
        SELECT * FROM scores
        WHERE endpoint_id = ?
        ORDER BY created_at DESC, id DESC
        """,
        (endpoint_id,),
    ).fetchall()


class BatchRequest(BaseModel):
    endpoint_ids: list[str]


@app.get("/v1/score/{endpoint_id}")
def get_score(endpoint_id: str):
    with get_conn() as conn:
        rows = fetch_endpoint_rows(conn, endpoint_id)
        if not rows:
            raise HTTPException(status_code=404, detail=f"No score found for endpoint_id '{endpoint_id}'")
        result = summarize_endpoint(rows)
        if posthog_client:
            posthog_client.capture(
                endpoint_id,
                "trust_score_queried",
                properties={
                    "endpoint_id": endpoint_id,
                    "trust_score": result["trust_score"],
                    "grade": result["grade"],
                    "escrow_recommendation": result["escrow_recommendation"],
                },
            )
        return result


@app.get("/v1/scores")
def list_scores():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM scores ORDER BY endpoint_id, created_at DESC, id DESC"
        ).fetchall()
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault(row["endpoint_id"], []).append(row)
    results = [summarize_endpoint(endpoint_rows) for endpoint_rows in groups.values()]
    results.sort(key=lambda r: r["trust_score"], reverse=True)
    return results


@app.post("/v1/score/batch")
def batch_scores(req: BatchRequest):
    with get_conn() as conn:
        results = []
        for endpoint_id in req.endpoint_ids:
            rows = fetch_endpoint_rows(conn, endpoint_id)
            if rows:
                results.append(summarize_endpoint(rows))
        return results
