"""Integration test for the full Beacon demo flow. Requires `python run.py`
running (locally or deployed). Tests go through the public gateway, the
same path a real browser/client uses — internal services aren't reachable
directly once deployed.

Usage:
  python test_end_to_end.py                  # tests http://localhost:8080
  BASE_URL=https://your-app.fly.dev python test_end_to_end.py
"""
import os
import sys
import time

import httpx

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8080").rstrip("/")
PROBE_BASE = f"{BASE_URL}/api/probe"
LEDGER_BASE = f"{BASE_URL}/api/ledger"
ESCROW_BASE = f"{BASE_URL}/api/escrow"

GREEN = "\033[92m"
RED = "\033[91m"
RESET = "\033[0m"

passed = 0
failed = 0


def check(label: str, condition: bool, detail: str = ""):
    global passed, failed
    if condition:
        passed += 1
        print(f"{GREEN}PASS{RESET}  {label}")
    else:
        failed += 1
        print(f"{RED}FAIL{RESET}  {label}  {detail}")


def main():
    # /v1/probe can take up to ~30s when NOUS_API_KEY is set and a probe
    # actually triggers a live Nemotron 3 Ultra evaluation.
    client = httpx.Client(timeout=45.0)

    # 1. POST to /v1/probe with a synthetic target
    probe_payload = {
        "target_url": "https://postman-echo.com/post",
        "task_description": "Echo test payload back",
        "payload": {"hello": "beacon"},
        "ground_truth": "hello beacon",
        "spend_amount_cents": 1,
    }
    try:
        resp = client.post(f"{PROBE_BASE}/v1/probe", json=probe_payload)
        probe_ok = resp.status_code == 200
        body = resp.json() if probe_ok else {}
    except httpx.HTTPError as e:
        probe_ok = False
        body = {}
        resp = None

    check("POST /v1/probe returns 200", probe_ok, f"status={getattr(resp, 'status_code', None)}")

    # 2. Verify response has trust_score, grade, attestation
    has_fields = all(k in body for k in ("trust_score", "grade", "attestation"))
    check("/v1/probe response has trust_score, grade, attestation", has_fields, str(body)[:200])

    endpoint_id = "postman-echo-com"

    # 3. GET /v1/score/{endpoint_id} from ledger and verify it matches
    try:
        score_resp = client.get(f"{LEDGER_BASE}/v1/score/{endpoint_id}")
        score_ok = score_resp.status_code == 200
        score_body = score_resp.json() if score_ok else {}
    except httpx.HTTPError:
        score_ok = False
        score_body = {}

    matches = score_ok and has_fields and score_body.get("trust_score") == body.get("trust_score")
    check("GET /v1/score/{endpoint_id} matches probe result", matches, str(score_body)[:200])

    # 4. POST /v1/escrow/validate with payment_amount_cents=5000
    try:
        escrow_resp = client.post(
            f"{ESCROW_BASE}/v1/escrow/validate",
            json={"endpoint_id": endpoint_id, "payment_amount_cents": 5000},
        )
        escrow_ok = escrow_resp.status_code == 200
        escrow_body = escrow_resp.json() if escrow_ok else {}
    except httpx.HTTPError:
        escrow_ok = False
        escrow_body = {}

    expected_can_pay = score_body.get("trust_score", 0) >= 70
    check(
        "/v1/escrow/validate can_pay reflects trust_score",
        escrow_ok and escrow_body.get("can_pay") == expected_can_pay,
        str(escrow_body)[:200],
    )

    # 5. POST /v1/escrow/validate with a known low-score endpoint -> can_pay false
    try:
        low_resp = client.post(
            f"{ESCROW_BASE}/v1/escrow/validate",
            json={"endpoint_id": "api-scamcoin-signals-net", "payment_amount_cents": 5000},
        )
        low_ok = low_resp.status_code == 200
        low_body = low_resp.json() if low_ok else {}
    except httpx.HTTPError:
        low_ok = False
        low_body = {}

    check("/v1/escrow/validate blocks low-score endpoint", low_ok and low_body.get("can_pay") is False, str(low_body)[:200])

    # 6. Exhaust the daily budget and verify 403 (daily spend guardrail)
    guardrail_triggered = False
    last_resp = None
    for i in range(15):
        try:
            r = client.post(
                f"{PROBE_BASE}/v1/probe",
                json={**probe_payload, "spend_amount_cents": 400},
            )
        except httpx.HTTPError:
            break
        last_resp = r
        if r.status_code == 403:
            guardrail_triggered = True
            break

    check(
        "Daily spend guardrail returns 403 when budget exceeded",
        guardrail_triggered,
        f"last_status={getattr(last_resp, 'status_code', None)}",
    )

    print(f"\n{passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
