# Beacon — Hackathon Demo Stack

Trust scores for the agentic web: probe an API, score it, attest it,
and gate payments on the result. Four FastAPI services + a static demo
page, all wired together and runnable with one command on a single laptop.

## Run it

```bash
pip install -r requirements.txt
python init_databases.py && python run.py
```

Then open **http://localhost:8080** in your browser.

`init_databases.py` is idempotent — safe to re-run any time. It creates
`db/` and seeds 3 synthetic scores/attestations if the databases are empty.

`run.py` starts all 5 processes (4 APIs + static server) in one terminal
and shuts them all down cleanly on Ctrl+C.

## Port map

| Port | Service                  | File                        |
|------|---------------------------|------------------------------|
| 8000 | Probe Engine               | `services/probe_engine.py`  |
| 8001 | Ledger API (read-only)     | `services/ledger_api.py`    |
| 8002 | Attestation / Verification | `services/attestation.py`   |
| 8003 | Escrow Gate                | `services/escrow_gate.py`   |
| 8080 | Demo static site           | `static/demo.html`          |

## curl examples

**Probe a target API:**

```bash
curl -X POST http://localhost:8000/v1/probe \
  -H 'Content-Type: application/json' \
  -d '{
    "target_url": "https://httpbin.org/post",
    "task_description": "Echo test payload back",
    "payload": {"hello": "beacon"},
    "ground_truth": "hello beacon",
    "spend_amount_cents": 1
  }'
```

**Get a score from the ledger:**

```bash
curl http://localhost:8001/v1/score/httpbin-org
curl http://localhost:8001/v1/scores
```

**Validate an escrow payment:**

```bash
curl -X POST http://localhost:8003/v1/escrow/validate \
  -H 'Content-Type: application/json' \
  -d '{"endpoint_id": "api-weather-ai-com", "payment_amount_cents": 5000}'
```

**Get / submit an attestation:**

```bash
curl -X POST http://localhost:8002/v1/attest \
  -H 'Content-Type: application/json' \
  -d '{"endpoint_id": "api-weather-ai-com", "trust_score": 94, "validator_id": "beacon-avs-v2"}'

curl http://localhost:8002/v1/verify/0x<attestation>
```

**Health check (any service):**

```bash
curl http://localhost:8000/health
```

## Run the integration test

With the stack running (`python run.py` in another terminal):

```bash
python test_end_to_end.py
```

Prints a green `PASS`/red `FAIL` per step and exits 0 only if everything
passes — probe → score → escrow pass → escrow block → NemoClaw guardrail.

## 90-second demo script

1. **(0:00–0:15)** Open `http://localhost:8080`. Point out the 4 green
   health dots — the whole trust stack is live. Scroll to the scoreboard:
   3 seeded endpoints, graded A/D/F.
2. **(0:15–0:35)** Click into the **Probe Console**. Paste a target URL
   (`https://httpbin.org/post`), hit **Probe Now**. A fresh score card
   appears in real time — trust score, grade, attestation hash, all
   computed live by `probe_engine.py` and written to `probes.db`.
3. **(0:35–0:55)** Click **Pay $50** on the high-trust (A grade) card —
   the escrow modal shows **Payment Released**. Click **Pay $50** on the
   F-grade scam-flagged card — modal shows **Payment Blocked**, citing
   the trust score. This is the escrow gate calling the ledger live.
4. **(0:55–1:15)** Mention the **NemoClaw guardrail**: probe spend is
   capped at $10/day; once exhausted, `/v1/probe` returns 403 instead of
   silently overspending — show `python test_end_to_end.py` passing,
   including the guardrail test.
5. **(1:15–1:30)** Close with the architecture: 4 independently
   verifiable planes (probe, ledger, verification/AVS, escrow) — each
   could be slashed/audited independently in a real EigenLayer AVS.

## Troubleshooting

- **Port already in use**: another process is bound to 8000-8003 or
  8080. Find and kill it: `lsof -ti:8000 | xargs kill`, repeat per port.
- **CORS errors in the browser console**: all 4 services have
  `CORSMiddleware(allow_origins=["*"])` — if you still see CORS errors,
  confirm you're hitting the right port and that the service actually
  started (check the `run.py` terminal output for the `Started ... on
  port ...` lines).
- **`database is locked`**: SQLite serializes writers. All services
  retry with exponential backoff (up to 3 tries) on `OperationalError`.
  If it persists, make sure you don't have a second `run.py` instance
  running against the same `db/` directory.
- **Demo page shows "offline · showing seed data"**: the ledger API
  (port 8001) isn't reachable — check it started cleanly and that
  `db/probes.db` exists (`python init_databases.py`).
- **Re-seeding**: `init_databases.py` skips seeding if the relevant
  table already has rows. To force a fresh demo dataset, delete the
  `db/` directory and re-run `python init_databases.py`.
