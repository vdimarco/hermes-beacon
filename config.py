"""Shared configuration for all Beacon services."""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Internal services bind to 127.0.0.1 and are only reachable from the
# gateway process running in the same container/VM. Override the host via
# env vars if a service ever needs to run on a separate machine.
PROBE_ENGINE_URL = os.environ.get("PROBE_ENGINE_URL", "http://127.0.0.1:8000")
LEDGER_API_URL = os.environ.get("LEDGER_API_URL", "http://127.0.0.1:8001")
ATTESTATION_URL = os.environ.get("ATTESTATION_URL", "http://127.0.0.1:8002")
ESCROW_GATE_URL = os.environ.get("ESCROW_GATE_URL", "http://127.0.0.1:8003")

# Public-facing gateway port. Fly.io injects PORT; defaults to 8080 locally.
GATEWAY_PORT = int(os.environ.get("PORT", "8080"))

DAILY_PROBE_BUDGET_CENTS = int(os.environ.get("DAILY_PROBE_BUDGET_CENTS", "1000"))  # $10.00/day

# DATA_DIR points at a persistent volume in production (e.g. Fly volume
# mounted at /data). Falls back to ./db for local development.
DATABASE_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "db"))

PROBES_DB_PATH = os.path.join(DATABASE_DIR, "probes.db")
ATTESTATIONS_DB_PATH = os.path.join(DATABASE_DIR, "attestations.db")
DISPUTES_DB_PATH = os.path.join(DATABASE_DIR, "disputes.db")
ESCROW_DB_PATH = os.path.join(DATABASE_DIR, "escrow.db")

HTTP_TIMEOUT_SECONDS = 5.0

# Comma-separated list of allowed origins for CORS. "*" for local dev.
CORS_ALLOW_ORIGINS = os.environ.get("CORS_ALLOW_ORIGINS", "*").split(",")
