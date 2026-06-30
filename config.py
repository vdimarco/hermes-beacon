"""Shared configuration for all Beacon services."""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

PROBE_ENGINE_URL = "http://localhost:8000"
LEDGER_API_URL = "http://localhost:8001"
ATTESTATION_URL = "http://localhost:8002"
ESCROW_GATE_URL = "http://localhost:8003"
DEMO_SERVER_URL = "http://localhost:8080"

DAILY_PROBE_BUDGET_CENTS = 1000  # $10.00/day

DATABASE_DIR = os.path.join(BASE_DIR, "db")

PROBES_DB_PATH = os.path.join(DATABASE_DIR, "probes.db")
ATTESTATIONS_DB_PATH = os.path.join(DATABASE_DIR, "attestations.db")
DISPUTES_DB_PATH = os.path.join(DATABASE_DIR, "disputes.db")
ESCROW_DB_PATH = os.path.join(DATABASE_DIR, "escrow.db")

HTTP_TIMEOUT_SECONDS = 5.0
