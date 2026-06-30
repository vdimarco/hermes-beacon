"""Hermes-as-orchestrator: picks a probe candidate, scores it through Beacon,
gates spend through Beacon's escrow, and (if configured) actually pays via
the Stripe Link CLI skill and alerts an operator via a Stripe Projects
-provisioned Twilio number on BLOCK.

This is a standalone client of the gateway, like scripts/competitor_intel.py
and scripts/seed_data.py -- it does not add a 5th service and isn't touched
by run.py.

Three distinct Stripe surfaces are involved, none of which collapse into
each other:
  1. Beacon's own escrow_gate.py -- merchant-side: creates/captures a
     Stripe PaymentIntent against Beacon's own Stripe account.
  2. Stripe Link CLI (@stripe/link-cli) -- buyer-side: this script, acting
     as the agent, requests approval and spends through the operator's
     Link wallet to pay a *different* merchant. Card data never enters
     this script's memory -- the CLI writes it straight to a temp file
     with 0600 perms, which is deleted immediately after use.
  3. Stripe Projects CLI (`stripe projects`) -- infra provisioning: used
     once, behind --provision, to add a Twilio SMS service to this
     project so BLOCK verdicts can page an operator. Real billing.

None of the `hermes` / `link-cli` / `stripe` binaries need to be installed
for this script to run -- each external call degrades to a clearly labeled
"not_configured" / "not_found" result instead of crashing, mirroring the
NOUS_API_KEY / STRIPE_SECRET_KEY fallback pattern already used by
services/probe_engine.py and services/escrow_gate.py.

Usage:
  python scripts/hermes_orchestrator.py --task "find a weather API and verify it"
  python scripts/hermes_orchestrator.py --task "..." --provision   # also sets up Twilio alerts
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8080").rstrip("/")
PROBE_BASE = f"{BASE_URL}/api/probe"
ESCROW_BASE = f"{BASE_URL}/api/escrow"

TARGETS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "targets.json")
PROJECTS_VAULT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".projects", "vault", "vault.json")
ENV_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")

DEFAULT_SPEND_CENTS = 5000  # $50, matches the demo's "Pay $50" buttons


def endpoint_id_from_url(url: str) -> str:
    """Mirrors services/probe_engine.py's endpoint_id_from_url() -- the
    ledger/escrow services key scores by hostname-derived slug, not by
    whatever id a candidate list happens to use."""
    try:
        hostname = httpx.URL(url).host or url
    except Exception:
        hostname = url
    return hostname.replace(".", "-")


def load_candidates() -> list[dict]:
    if not os.path.exists(TARGETS_FILE):
        raise FileNotFoundError(
            f"{TARGETS_FILE} not found -- run scripts/competitor_intel.py first to discover candidates."
        )
    with open(TARGETS_FILE) as f:
        return json.load(f)


def pick_with_hermes(task: str, candidates: list[dict]) -> tuple[dict | None, str]:
    """Asks the real Hermes CLI to choose a candidate for this task. Returns
    (chosen_candidate_or_None, picker_label) so callers can see whether
    Hermes actually ran or the deterministic fallback did."""
    prompt = (
        f"Task: {task}\n\n"
        f"Candidates (JSON array, each has endpoint_id/url/task_description/ground_truth):\n"
        f"{json.dumps(candidates)}\n\n"
        "Pick exactly one candidate worth probing for this task. "
        'Reply with ONLY a JSON object: {"endpoint_id": "...", "url": "...", '
        '"task_description": "...", "ground_truth": "...", "payload": {}}'
    )
    try:
        result = subprocess.run(
            [config.HERMES_CLI_PATH, "chat", "-q", prompt, "--toolsets", "web,terminal"],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except FileNotFoundError:
        return None, "hermes_not_found"
    except subprocess.TimeoutExpired:
        return None, "hermes_timeout"

    if result.returncode != 0:
        return None, "hermes_error"

    try:
        start = result.stdout.find("{")
        end = result.stdout.rfind("}")
        choice = json.loads(result.stdout[start:end + 1])
        return choice, "hermes"
    except (ValueError, json.JSONDecodeError):
        return None, "hermes_unparseable_output"


def pick_fallback(candidates: list[dict]) -> dict:
    """Deterministic picker used when the hermes CLI isn't available --
    same role as call_nemotron_mock() in services/probe_engine.py."""
    return candidates[0]


def run_probe(candidate: dict) -> dict:
    resp = httpx.post(
        f"{PROBE_BASE}/v1/probe",
        json={
            "target_url": candidate["url"],
            "task_description": candidate["task_description"],
            "payload": candidate.get("payload", {}),
            "ground_truth": candidate["ground_truth"],
            "spend_amount_cents": candidate.get("spend_amount_cents", 3),
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()


def run_escrow_validate(endpoint_id: str, amount_cents: int) -> dict:
    resp = httpx.post(
        f"{ESCROW_BASE}/v1/escrow/validate",
        json={"endpoint_id": endpoint_id, "payment_amount_cents": amount_cents},
        timeout=15.0,
    )
    resp.raise_for_status()
    return resp.json()


def link_cli(*args: str, timeout: float = 30.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*config.STRIPE_LINK_CLI_CMD, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def spend_via_link(endpoint_id: str, amount_cents: int) -> dict:
    """Real Stripe Link CLI buyer-side flow. Returns a dict describing what
    happened; never raises for "not installed" -- that's an expected,
    labeled outcome in this sandbox."""
    try:
        auth = link_cli("auth", "status", "--format", "json")
    except FileNotFoundError:
        return {"status": "not_configured", "detail": "link-cli not installed (requires Node 20+)"}
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "detail": "link-cli auth status timed out"}

    if auth.returncode != 0:
        return {"status": "not_authenticated", "detail": auth.stderr.strip()[:300]}

    create = link_cli(
        "spend-request", "create",
        "--merchant", endpoint_id,
        "--amount", str(amount_cents),
        "--request-approval",
        "--format", "json",
        timeout=300.0,  # human approval happens in the Link app
    )
    if create.returncode != 0:
        return {"status": "spend_request_failed", "detail": create.stderr.strip()[:300]}

    try:
        spend_request = json.loads(create.stdout)
        spend_request_id = spend_request["id"]
    except (ValueError, KeyError, json.JSONDecodeError):
        return {"status": "spend_request_unparseable", "detail": create.stdout[:300]}

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        card_path = tmp.name
    try:
        retrieve = link_cli(
            "spend-request", "retrieve", spend_request_id,
            "--include", "card",
            "--output-file", card_path,
        )
        if retrieve.returncode != 0:
            return {"status": "retrieve_failed", "spend_request_id": spend_request_id, "detail": retrieve.stderr.strip()[:300]}
        # Card PAN intentionally never read into this process -- the file
        # path itself is the handoff point to whatever checkout consumes it.
        return {"status": "approved", "spend_request_id": spend_request_id, "card_file": card_path}
    finally:
        if os.path.exists(card_path):
            os.remove(card_path)


def provision_twilio_alerts() -> dict:
    """One-time Stripe Projects setup for a Twilio SMS alert channel.
    Real billing confirmation is left to the interactive `stripe` CLI
    prompt -- this script never auto-confirms a charge."""
    try:
        if not os.path.exists(PROJECTS_VAULT):
            init = subprocess.run(
                [config.STRIPE_PROJECTS_CLI_PATH, "projects", "init"],
                cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                timeout=60,
            )
            if init.returncode != 0:
                return {"status": "init_failed"}
        add = subprocess.run(
            [config.STRIPE_PROJECTS_CLI_PATH, "projects", "add", "twilio/sms"],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            timeout=300,  # interactive billing-confirmation prompt
        )
        return {"status": "provisioned" if add.returncode == 0 else "add_failed"}
    except FileNotFoundError:
        return {"status": "not_configured", "detail": "stripe CLI not installed"}
    except subprocess.TimeoutExpired:
        return {"status": "timeout"}


def _read_env_var(name: str) -> str | None:
    if not os.path.exists(ENV_FILE):
        return None
    with open(ENV_FILE) as f:
        for line in f:
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip()
    return None


def send_alert(message: str) -> dict:
    """Sends one SMS via Twilio's REST API using credentials that
    `stripe projects add twilio/sms` synced into .env. Plain httpx call --
    no new dependency needed."""
    account_sid = _read_env_var("TWILIO_ACCOUNT_SID")
    auth_token = _read_env_var("TWILIO_AUTH_TOKEN")
    from_number = _read_env_var("TWILIO_PHONE_NUMBER")

    if not (account_sid and auth_token and from_number and config.ALERT_PHONE_NUMBER):
        return {"status": "not_configured"}

    try:
        resp = httpx.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json",
            auth=(account_sid, auth_token),
            data={"From": from_number, "To": config.ALERT_PHONE_NUMBER, "Body": message},
            timeout=10.0,
        )
        return {"status": "sent" if resp.status_code < 300 else "failed", "http_status": resp.status_code}
    except httpx.HTTPError as e:
        return {"status": "failed", "detail": str(e)[:200]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", required=True, help="What the orchestrator should find/verify")
    parser.add_argument("--provision", action="store_true", help="Run Stripe Projects setup for Twilio alerts first")
    parser.add_argument("--amount-cents", type=int, default=DEFAULT_SPEND_CENTS)
    args = parser.parse_args()

    summary: dict = {"task": args.task}

    if args.provision:
        summary["provisioning"] = provision_twilio_alerts()

    candidates = load_candidates()
    choice, picker = pick_with_hermes(args.task, candidates)
    if choice is None:
        choice = pick_fallback(candidates)
    summary["picker"] = picker
    # Ledger/escrow key scores by hostname-derived slug, not by whatever id
    # the candidate list happens to use -- compute the same slug they will.
    endpoint_id = endpoint_id_from_url(choice["url"])
    summary["candidate"] = {"endpoint_id": endpoint_id, "url": choice["url"]}

    probe_result = run_probe(choice)
    summary["probe"] = {
        "trust_score": probe_result.get("trust_score"),
        "grade": probe_result.get("grade"),
        "evaluator": probe_result.get("evaluator"),
    }

    escrow_result = run_escrow_validate(endpoint_id, args.amount_cents)
    summary["escrow"] = {
        "can_pay": escrow_result.get("can_pay"),
        "reason": escrow_result.get("reason"),
        "stripe_payment_intent_id": escrow_result.get("stripe_payment_intent_id"),
    }

    if escrow_result.get("can_pay"):
        link_result = spend_via_link(endpoint_id, args.amount_cents)
        summary["stripe_link"] = link_result
        if link_result["status"] not in ("approved",):
            summary["alert"] = send_alert(
                f"Beacon: Link spend failed for {endpoint_id} ({link_result['status']})"
            )
    else:
        summary["stripe_link"] = {"status": "skipped_block"}
        summary["alert"] = send_alert(
            f"Beacon: escrow BLOCKED payment to {endpoint_id} -- {escrow_result.get('reason')}"
        )

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
