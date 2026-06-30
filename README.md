# Beacon — Trust Scores for the Agentic Web

Trust scores for the agentic web: probe an API, score it, attest it,
and gate payments on the result. Four FastAPI services behind a single
public gateway, with persistent SQLite storage. Runs locally with one
command, or deployed on Fly.io.

**Live:** https://hermes-beacon.fly.dev

## Architecture

```
                         ┌─────────────────────────┐
  browser  ───────────▶  │   gateway (port 8080)    │  ← only public process
                         │   static site + /api/*   │
                         └───────────┬──────────────┘
                                     │ proxies to 127.0.0.1 (internal-only)
              ┌──────────────┬───────┴────────┬──────────────┐
              ▼              ▼                ▼              ▼
        probe_engine    ledger_api      attestation     escrow_gate
          :8000           :8001            :8002           :8003
              │              │                │              │
              └──────┬───────┘                └──────┬───────┘
                      ▼                               ▼
                 db/probes.db                  db/attestations.db
                                                db/disputes.db
                                                db/escrow.db
```

The browser only ever talks to the gateway (`/`, `/api/probe/*`,
`/api/ledger/*`, `/api/attestation/*`, `/api/escrow/*`, `/health`). The
4 backend services bind to `127.0.0.1` and are unreachable from outside
the container — there's nothing else to lock down or CORS-restrict.

## Run it locally

```bash
pip install -r requirements.txt
python init_databases.py && python run.py
```

Then open **http://localhost:8080** in your browser.

`init_databases.py` is idempotent — safe to re-run any time. It creates
`db/` and seeds 3 synthetic scores/attestations if the databases are empty.

`run.py` starts all 5 processes (4 internal APIs + the public gateway)
in one terminal and shuts them all down cleanly on Ctrl+C.

## Deploying to Fly.io

The app is already provisioned (`hermes-beacon`, region `iad`) with a
1GB persistent volume (`beacon_data`) mounted at `/data`, so the SQLite
databases survive restarts and redeploys.

```bash
flyctl deploy
```

Config:
- `Dockerfile` — builds the image, runs `init_databases.py && python run.py` as the container CMD.
- `fly.toml` — `http_service` exposes only the gateway's port 8080 (mapped to 443); `[[mounts]]` attaches the persistent volume at `/data`; `DATA_DIR=/data/db` (set in the Dockerfile) points all 4 services' SQLite paths at the volume.
- The app scales to zero when idle (`min_machines_running = 0`) and auto-starts on the next request — cheap for a demo, with a few seconds of cold-start latency on the first hit after idling.

To check on it:

```bash
flyctl status -a hermes-beacon
flyctl logs -a hermes-beacon
```

## Port map (local dev)

| Port | Service                     | File                          | Reachable from |
|------|------------------------------|---------------------------------|-----------------|
| 8080 | Gateway (static + /api/* proxy) | `services/gateway.py`        | public |
| 8000 | Probe Engine                 | `services/probe_engine.py`      | internal only |
| 8001 | Ledger API (read-only)       | `services/ledger_api.py`        | internal only |
| 8002 | Attestation / Verification   | `services/attestation.py`       | internal only |
| 8003 | Escrow Gate                  | `services/escrow_gate.py`       | internal only |

In production (Fly.io) only the gateway's port is exposed at all —
ports 8000-8003 don't have a public route.

## curl examples

Examples below use `http://localhost:8080` (swap in `https://hermes-beacon.fly.dev` to hit the live deployment).

**Probe a target API:**

```bash
curl -X POST http://localhost:8080/api/probe/v1/probe \
  -H 'Content-Type: application/json' \
  -d '{
    "target_url": "https://httpstat.us/200",
    "task_description": "Echo test payload back",
    "payload": {"hello": "beacon"},
    "ground_truth": "hello beacon",
    "spend_amount_cents": 1
  }'
```

**Get a score from the ledger:**

```bash
curl http://localhost:8080/api/ledger/v1/score/httpbin-org
curl http://localhost:8080/api/ledger/v1/scores
```

**Validate an escrow payment:**

```bash
curl -X POST http://localhost:8080/api/escrow/v1/escrow/validate \
  -H 'Content-Type: application/json' \
  -d '{"endpoint_id": "api-weather-ai-com", "payment_amount_cents": 5000}'
```

**Get / submit an attestation:**

```bash
curl -X POST http://localhost:8080/api/attestation/v1/attest \
  -H 'Content-Type: application/json' \
  -d '{"endpoint_id": "api-weather-ai-com", "trust_score": 94, "validator_id": "beacon-avs-v2"}'

curl http://localhost:8080/api/attestation/v1/verify/0x<attestation>
```

**Health check (gateway aggregates all 4 services):**

```bash
curl http://localhost:8080/health
```

(Local dev only, direct service health checks: `curl http://localhost:8000/health`, etc.)

## Run the integration test

Against local dev (with `python run.py` running in another terminal):

```bash
python test_end_to_end.py
```

Against the live deployment:

```bash
BASE_URL=https://hermes-beacon.fly.dev python test_end_to_end.py
```

Prints a green `PASS`/red `FAIL` per step and exits 0 only if everything
passes — probe → score → escrow pass → escrow block → NemoClaw guardrail.

## 90-second demo script

1. **(0:00–0:15)** Open the live URL. Point out the 4 green health dots —
   the whole trust stack is live. Scroll to the scoreboard: 3 seeded
   endpoints, graded A/D/F.
2. **(0:15–0:35)** Click into the **Probe Console**. Paste a target URL
   (`https://httpstat.us/200`), hit **Probe Now**. A fresh score card
   appears in real time — trust score, grade, attestation hash, all
   computed live by `probe_engine.py` and written to persistent storage.
3. **(0:35–0:55)** Click **Pay $50** on the high-trust (A grade) card —
   the escrow modal shows **Payment Released**. Click **Pay $50** on the
   F-grade scam-flagged card — modal shows **Payment Blocked**, citing
   the trust score. This is the escrow gate calling the ledger live.
4. **(0:55–1:15)** Mention the **NemoClaw guardrail**: probe spend is
   capped at $10/day; once exhausted, `/v1/probe` returns 403 instead of
   silently overspending — show `python test_end_to_end.py` passing,
   including the guardrail test.
5. **(1:15–1:30)** Close with the architecture: 4 independently
   verifiable planes (probe, ledger, verification/AVS, escrow) behind one
   gateway — each plane could be slashed/audited independently in a real
   EigenLayer AVS.

## What's still mocked / demo-only

This is a real, persistent backend, not a toy — but a few pieces are
intentionally simulated for the hackathon:

- **Nemotron scoring** falls back to a deterministic mock evaluator
  unless `NVIDIA_API_KEY` is set (`services/probe_engine.py`).
- **Escrow/Stripe** is simulated bookkeeping (`escrow.db`), not real
  payment movement.
- **No auth/rate limiting** on any endpoint — anyone with the URL can
  call `/v1/probe` or `/v1/escrow/validate`.

## Troubleshooting

- **Port already in use (local dev)**: another process is bound to
  8000-8003 or 8080. Find and kill it: `lsof -ti:8000 | xargs kill`,
  repeat per port.
- **`database is locked`**: SQLite serializes writers. All services
  retry with exponential backoff (up to 3 tries) on `OperationalError`.
  If it persists, make sure you don't have a second `run.py` instance
  running against the same data directory.
- **Demo page shows "offline · showing seed data"**: the gateway can't
  reach the ledger service. Check `curl <base>/health` to see which
  upstream is down.
- **Re-seeding**: `init_databases.py` skips seeding if the relevant
  table already has rows. To force a fresh demo dataset locally, delete
  the `db/` directory and re-run `python init_databases.py`. On Fly,
  you'd need to clear the volume (`flyctl ssh console` and remove the
  files under `/data/db`).
- **Fly deploy fails with a registry 401**: this is a known transient
  issue with Fly's remote Depot builder — retry `flyctl deploy`. Make
  sure `flyctl` is reasonably current (`brew upgrade flyctl`); older
  versions hit this more often.
