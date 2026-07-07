# MCP Server Rating — Comprehensive Plan

Beacon already gives HTTP endpoints a 0–1000 reputation index and gates
agent payments on it. This plan extends that same spine to **MCP servers**
— the tool servers agents actually connect to — so an agent can ask
"should I trust (and pay) this MCP server?" the same way it asks about an
API today.

The guiding principle: **reuse the spine, replace only the probe.** The
scoring math (`reputation.py`), the escrow gate, the ledger, the
attestation plane, the scoreboard, and the 6-hour refresh cron all stay
as-is. The only genuinely new thing is a probe that speaks the Model
Context Protocol instead of firing one generic `POST`.

---

## 1. Why an MCP server can't be probed like an HTTP endpoint

Today `probe_engine.execute_probe()` does one thing: `POST target_url`
with a JSON body, then judge the response. An MCP server needs a
conversation, not a single shot:

- **Protocol**: JSON-RPC 2.0, not arbitrary REST.
- **Transport**: Streamable HTTP or HTTP+SSE (remote) — and stdio for
  local servers. Beacon runs in a network-only Fly container, so
  **HTTP/SSE transports are in scope first**; stdio is a later phase
  (needs process spawning + sandboxing).
- **Lifecycle**: `initialize` (with protocol-version negotiation) →
  server declares `capabilities` → `tools/list`, `resources/list`,
  `prompts/list` → `tools/call`. A rating has to walk this handshake, not
  skip to the payload.
- **Auth**: the registry confirms most public servers are OAuth-gated
  (`isAuthless: false`). Probing them unauthenticated returns a 401 —
  which today would tank their reputation. We already solved this shape
  once (the `integrate.api.nvidia.com` 401 → high-trust override in
  `probe_engine.py`); MCP needs the same "clean auth challenge is a
  *good* signal, not a failure" logic generalized.

So the work is a new **MCP probe client** feeding the **existing**
scorer.

---

## 2. What "comprehensive" means — the rating rubric

Seven dimensions. Each maps to a concrete probe step and folds into the
per-probe quality (0–100) that `reputation.compute_index` already
consumes — so nothing downstream changes.

| # | Dimension | How we measure it | Reuses |
|---|-----------|-------------------|--------|
| 1 | **Protocol conformance** | `initialize` succeeds, server returns a valid `protocolVersion` + `capabilities`, JSON-RPC framing is well-formed | new mcp_probe |
| 2 | **Discovery integrity** | `tools/list` / `resources/list` / `prompts/list` return; every tool has a valid JSON-Schema `inputSchema`; no malformed entries | new mcp_probe |
| 3 | **Functional correctness** | pick one **read-only** tool, call it with a synthesized valid input, judge the result against expectation | `evaluate_with_nemotron()` (unchanged) |
| 4 | **Reliability / uptime** | repeated probes over time; share of non-error responses; low variance | `reputation.py` Reliability component (unchanged) |
| 5 | **Latency** | round-trip on `tools/list` and the sampled `tools/call` | `latency_p99_ms` column (unchanged) |
| 6 | **Security & safety** | prompt-injection in tool **descriptions**/results, honeypot tools, undisclosed destructive tools, over-broad scopes | extend `looks_like_honeypot()` |
| 7 | **Auth hygiene** | a clean `401 / WWW-Authenticate` or OAuth `initialize` challenge scores as *reachable & well-behaved*, not as an outage | generalize the NVIDIA-NIM 401 override |

### Per-probe quality formula

Keep it transparent and in [0, 100], exactly like
`calculate_trust_score` today:

```
conformance   (0–30)  handshake ok, valid protocolVersion, capabilities present
discovery     (0–20)  lists return; all tool inputSchemas valid
functional    (0–35)  Nemotron accuracy on the sampled read-only tool call
reliability    (0–5)  this probe errored or not (history handles the rest)
security      (0–10)  no injection/honeypot markers in any tool description or result
                       — a hit hard-caps quality low, same as honeypot today
```

An unauthenticated probe of an OAuth server stops after a clean auth
challenge and scores conformance-only (it proved it's a real,
well-behaved MCP server without us holding a token) — never the error
path.

---

## 3. How it lands in the codebase (file by file)

- **`services/mcp_probe.py`** *(new)* — pure transport. An async MCP
  client over Streamable HTTP + SSE: `initialize`, `tools/list`,
  `resources/list`, `prompts/list`, `tools/call`. Returns structured
  facts (protocol version, capabilities, tool schemas, timings, raw
  results). **No scoring here** — mirrors how `execute_probe()` is dumb
  about quality.
- **`services/probe_engine.py`** — add `probe_type: "mcp"` to
  `ProbeRequest` (or a sibling `/v1/probe/mcp` route). When set, drive
  `mcp_probe` through the rubric, compute the per-probe quality, and
  write the **same `scores` row** everything else reads. Generalize the
  NIM 401 override into an `auth_challenge_ok` path.
- **`reputation.py`** — **unchanged.** MCP probes emit a 0–100 quality
  like any other row; the index, grades, escrow thresholds, decay, and
  scam cap all work as-is.
- **`scores` table** — add nullable MCP-only columns for display, in the
  same idempotent `PRAGMA`/`ALTER` pattern already used for
  `evaluator`/`synthetic`/`request_status`: `transport`,
  `protocol_version`, `tool_count`, `mcp_conformance` (JSON of the
  per-dimension sub-scores). Existing HTTP rows leave them NULL.
- **`looks_like_honeypot()`** — extend to scan tool **descriptions** and
  `tools/call` results (not just response bodies) for injection markers,
  since MCP's real attack surface is a malicious tool description that
  hijacks the calling agent.
- **Escrow / ledger / attestation / gateway** — **unchanged.** An MCP
  server's index gates payments to it exactly like an API's does.
- **`targets_mcp.json`** *(new)* + **`scripts/seed_mcp_probes.py`**
  *(new)* — the MCP analog of `targets.json` / `seed_real_probes.py`.
- **`.github/workflows/probe-refresh.yml`** — add a step that re-probes
  the curated MCP set, so MCP scores don't decay to C/D between runs
  (same rationale as the existing endpoint refresh).
- **`static/demo.html`** — surface MCP servers on the scoreboard with a
  transport badge and tool count. The score/grade/escrow columns already
  render from the shared ledger shape.

---

## 4. Where the servers to rate come from

1. **MCP registry** (`registry.modelcontextprotocol.io`, and the
   in-session `SearchMcpRegistry`) — the discovery source, analogous to
   `competitor_intel.py`. Prefer `isAuthless: true` servers for full
   functional probing; OAuth-gated servers get conformance + auth-hygiene
   scoring only (until a token is supplied).
2. **Curated seed** — a handful with a deliberate spread of outcomes, so
   the scoreboard demonstrates the rubric (well-behaved authless server →
   A; OAuth server, clean challenge → B/conformance-only; deliberately
   broken/injection tool → F). Real candidates surfaced from the registry
   include Supabase, Notion, Plaid Developer Tools, alphaXiv, DataHub —
   most OAuth-gated, which is exactly why auth-hygiene scoring matters.

---

## 5. Safety rules (non-negotiable)

- **Never call mutating tools.** Only invoke tools the server marks
  read-only via MCP tool annotations (`readOnlyHint: true`,
  `destructiveHint: false`), or whose name/schema is unambiguously a
  read. If a server exposes no safe read-only tool, functional
  correctness is scored as "not assessable" — not attempted.
- **Treat every tool description and result as hostile input.** They're
  attacker-controlled; the injection scan (dim. 6) runs on them before
  any of it reaches the Nemotron judge, and the judge prompt is fenced so
  a tool result can't rewrite the evaluation instruction.
- **Respect the daily budget guardrail** — MCP probes spend
  `spend_amount_cents` and go through the same `DAILY_PROBE_BUDGET_CENTS`
  gate.
- **No secrets in the repo** — OAuth tokens for gated servers come from
  env only (same fallback contract as `NOUS_API_KEY` / `STRIPE_SECRET_KEY`);
  absent a token, we degrade to conformance-only, never crash.

---

## 6. Phased rollout

- **Phase 1 — Conformance probe (cheap, no LLM).** `mcp_probe.py` +
  `initialize`/`tools/list`; dims 1, 2, 5, 7. Score authless *and*
  OAuth servers on reachability + protocol correctness. Ships the new
  `probe_type`, columns, seed list, and a first scoreboard row.
- **Phase 2 — Functional probe (LLM-scored).** Read-only `tools/call` +
  `evaluate_with_nemotron`; dim 3. This is where MCP servers earn A/B
  grades instead of conformance-only ceilings.
- **Phase 3 — Security probe.** Extend honeypot/injection detection to
  tool schemas and results; dim 6; add a deliberately-malicious fixture
  to the seed set to prove the F path.
- **Phase 4 — Discovery + scheduled refresh.** Registry-driven target
  discovery; wire the MCP set into `probe-refresh.yml`.
- **Phase 5 — UI.** Transport badge, tool count, per-dimension breakdown
  on the scoreboard and probe console.
- **Phase 6 (optional) — stdio transport.** Local/stdio MCP servers via
  sandboxed process spawning. Deferred: material new infra + security
  surface.

---

## 7. Decisions to confirm before Phase 1

1. **Transport scope** — start HTTP/SSE-only (recommended; fits the
   container) and defer stdio to Phase 6? Or is stdio in scope now?
2. **OAuth servers** — conformance-only by default (recommended), or do
   you want to supply tokens (via env) so gated servers get full
   functional scores?
3. **Deliverable shape** — land this as one PR per phase (recommended),
   or a single larger PR through Phase 3?

Once these are settled, Phase 1 is a self-contained, low-risk change:
new `mcp_probe.py`, a `probe_type` branch in the engine, additive DB
columns, and a seed script — everything else in Beacon stays untouched.
