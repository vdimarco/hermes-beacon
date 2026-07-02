# PostHog post-wizard report

The wizard has completed a deep integration of PostHog analytics across all four Beacon backend services. A shared `posthog_client.py` module initialises a `Posthog()` instance from environment variables and registers a graceful shutdown handler. Each service (`probe_engine`, `attestation`, `escrow_gate`, `ledger_api`) imports this client and calls `posthog_client.capture()` at the key business decision points. No existing architecture was changed — all additions are additive call sites.

| Event name | Description | File |
|---|---|---|
| `endpoint_probed` | An API endpoint was probed and received a trust score and grade from the evaluation pipeline. | `services/probe_engine.py` |
| `probe_budget_guardrail_triggered` | A probe request was rejected because the daily spend budget was exceeded. | `services/probe_engine.py` |
| `honeypot_detected` | A probed endpoint was flagged as a honeypot or prompt-injection attempt. | `services/probe_engine.py` |
| `evaluator_fallback` | The real Nemotron model was unavailable and the mock evaluator was used instead. | `services/probe_engine.py` |
| `attestation_issued` | An attestation was issued for an endpoint by a validator. | `services/attestation.py` |
| `attestation_verified` | An attestation hash was looked up and confirmed in the verification plane. | `services/attestation.py` |
| `dispute_filed` | A dispute was raised against an endpoint, triggering a re-probe. | `services/attestation.py` |
| `escrow_validated` | An escrow validation was run for an endpoint, resulting in a PASS or BLOCK decision. | `services/escrow_gate.py` |
| `escrow_executed` | A previously approved escrow hold was executed and payment released. | `services/escrow_gate.py` |
| `trust_score_queried` | A trust score was looked up from the ledger for a specific endpoint. | `services/ledger_api.py` |

## Next steps

We've built some insights and a dashboard to monitor key Beacon metrics:

- [Analytics basics (wizard) dashboard](https://us.posthog.com/project/493684/dashboard/1788188)
- [Endpoint Probes Over Time](https://us.posthog.com/project/493684/insights/PRjbZZRX)
- [Trust Score Grade Distribution](https://us.posthog.com/project/493684/insights/W4iQvxnp)
- [Escrow Decisions: PASS vs BLOCK](https://us.posthog.com/project/493684/insights/Bb6DD6la)
- [Security Events: Honeypots & Disputes](https://us.posthog.com/project/493684/insights/1NW0cAzi)
- [Payment Pipeline: Validated vs Executed](https://us.posthog.com/project/493684/insights/CwaqImzj)

## Verify before merging

- [ ] Run a full production build (the wizard only verified the files it touched) and fix any lint or type errors introduced by the generated code.
- [ ] Run the test suite — call sites that were rewritten or instrumented may need updated mocks or fixtures.
- [ ] Add `POSTHOG_PROJECT_TOKEN`, `POSTHOG_HOST`, and `POSTHOG_DISABLED` to `.env.example` and any bootstrap scripts so collaborators know what to set.

### Agent skill

We've left an agent skill folder in your project. You can use this context for further agent development when using Claude Code. This will help ensure the model provides the most up-to-date approaches for integrating PostHog.
