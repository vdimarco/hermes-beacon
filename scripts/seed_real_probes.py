"""Populates the scoreboard with real, live-probed entries instead of only
the fictional fixtures in seed_data.py.

Runs actual POST /v1/probe calls (through the gateway, like a real client
would) against a small set of public, no-auth-required test/echo APIs.
Because these go through the real probe pipeline, services/probe_engine.py
marks them synthetic=0 automatically -- they show up on the scoreboard
without a "sample data" badge, same as any other live probe.

Targets are deliberately a mix of outcomes, all real public APIs (verified
reachable -- httpbin.org and httpstat.us were tried and dropped, they
returned 503/connection errors from this environment):
  - postman-echo.com/post, jsonplaceholder.typicode.com/posts -- well-known
    public echo/REST-testing services; the payload comes back in the
    response, so accuracy scores high (A/PASS).
  - dog.ceo/api/breeds/image/random -- a real, well-known API that only
    supports GET; POSTing to it correctly fails verification (F/BLOCK) --
    an honest example of "real service, wrong call shape" rather than a
    fabricated scam.
  - reqres.in/api/users -- a real API that now requires an API key; POSTing
    without one correctly fails verification (F/BLOCK) -- an honest
    example of "real service, missing auth."

Usage:
  python scripts/seed_real_probes.py                 # probes http://localhost:8080
  BASE_URL=https://hermes.beacons.fyi python scripts/seed_real_probes.py
"""
import os
import sys

import httpx

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8080").rstrip("/")
PROBE_URL = f"{BASE_URL}/api/probe/v1/probe"

TARGETS = [
    {
        "target_url": "https://postman-echo.com/post",
        "task_description": "Echo test payload back",
        "payload": {"hello": "beacon"},
        "ground_truth": "hello beacon",
        "spend_amount_cents": 3,
    },
    {
        "target_url": "https://jsonplaceholder.typicode.com/posts",
        "task_description": "Echo test payload back",
        "payload": {"hello": "beacon"},
        "ground_truth": "hello beacon",
        "spend_amount_cents": 3,
    },
    {
        "target_url": "https://dog.ceo/api/breeds/image/random",
        "task_description": "Return a random dog image URL",
        "payload": {"hello": "beacon"},
        "ground_truth": "random dog image URL",
        "spend_amount_cents": 3,
    },
    {
        "target_url": "https://reqres.in/api/users",
        "task_description": "Create a user record and echo it back",
        "payload": {"hello": "beacon"},
        "ground_truth": "hello beacon",
        "spend_amount_cents": 3,
    },
]


def main() -> int:
    failures = 0
    with httpx.Client(timeout=30.0) as client:
        for target in TARGETS:
            print(f"Probing {target['target_url']} ...")
            try:
                resp = client.post(PROBE_URL, json=target)
                resp.raise_for_status()
            except httpx.HTTPError as e:
                print(f"  ! probe failed: {e}")
                failures += 1
                continue
            result = resp.json()
            print(f"  -> trust_score={result.get('trust_score')} grade={result.get('grade')} evaluator={result.get('evaluator')}")

    if failures:
        print(f"\n{failures} probe(s) failed to even reach the gateway.")
        return 1
    print(f"\nProbed {len(TARGETS)} real endpoints. Check GET {BASE_URL}/api/ledger/v1/scores.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
