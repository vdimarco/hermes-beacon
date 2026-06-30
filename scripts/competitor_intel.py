"""
Discovers public landing pages / API hints for a fixed list of hackathon
demo URLs and writes targets.json in the schema expected by probe_engine.py
(POST /v1/probe).

This script only performs read-only GET requests against public landing
pages / READMEs. It does not call any payment, escrow, or "spend gate"
endpoint, and it does not invoke probe_engine.py itself -- actually running
probes (which spend real guardrailed cents) is a separate, deliberate step
left to the operator.
"""

import json
import re

import requests

TIMEOUT_SECONDS = 8
USER_AGENT = "BeaconCompetitorIntel/0.1 (+read-only landing page check)"

TARGETS = [
    ("opengap-ai", "https://opengap-ai.vercel.app/", "OpenGap AI"),
    ("the-partenon", "https://hermespartenon.online/", "The Partenon"),
    ("partenon-github", "https://github.com/cuentadeservicio377-cell/partenon", "Partenon GitHub repo"),
    ("downtimemachine", "https://downtimemachine.com", "DowntimeMachine"),
    ("ronin-agent", "https://ronin-agent.vercel.app", "Ronin"),
    ("clipit", "https://clipit.dev", "Clipit"),
    ("three-ws", "https://three.ws", "three.ws"),
    ("headgate-caplifi", "https://headgate.caplifi.com", "CapliFi / Headgate"),
    ("hivemind-agent", "https://hivemind-agent.vercel.app", "HiveMind"),
]

# Things in page text/headers that hint at a documented, public API surface.
API_HINTS = re.compile(r"\bapi[_/-]?docs?\b|swagger|openapi|/v1/|/api/", re.IGNORECASE)


def fetch(url: str) -> requests.Response | None:
    try:
        return requests.get(
            url,
            timeout=TIMEOUT_SECONDS,
            headers={"User-Agent": USER_AGENT},
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        print(f"  ! request failed for {url}: {exc}")
        return None


def build_entry(endpoint_id: str, url: str, label: str) -> dict:
    print(f"Probing {label} ({url}) ...")
    resp = fetch(url)

    has_api_hint = bool(resp is not None and resp.ok and API_HINTS.search(resp.text))

    if has_api_hint:
        return {
            "endpoint_id": endpoint_id,
            "url": url,
            "probe_type": "api",
            "task_description": f"{label} exposes a documented API surface; verify it responds as advertised.",
            "payload": {},
            "ground_truth": "200 OK with a structured (JSON) response matching the documented API.",
            "spend_amount_cents": 3,
        }

    return {
        "endpoint_id": endpoint_id,
        "url": url,
        "probe_type": "health_check",
        "task_description": f"{label} landing page should be reachable and return a successful HTTP status.",
        "payload": {"method": "GET", "path": "/"},
        "ground_truth": "HTTP 200-299 response on GET /",
        "spend_amount_cents": 3,
    }


def main() -> None:
    results = [build_entry(*t) for t in TARGETS]

    with open("targets.json", "w") as f:
        json.dump(results, f, indent=2)
        f.write("\n")

    print(f"\nWrote {len(results)} entries to targets.json")


if __name__ == "__main__":
    main()
