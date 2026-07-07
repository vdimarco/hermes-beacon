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
- **Auth**: most public remote servers are auth-gated — the official
  registry marks them with required `headers`, and Claude's connector
  directory with `isAuthless: false` (§4). Probing them unauthenticated
  returns a 401, which today would tank their reputation. We already
  solved this shape once (the `integrate.api.nvidia.com` 401 → high-trust
  override in `probe_engine.py`); MCP needs the same "clean auth challenge
  is a *good* signal, not a failure" logic generalized.

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

Discovery is not the hard part; **the addressable set for *functional*
probing is.** Both need to be stated honestly.

### Primary source — the official MCP Registry

`https://registry.modelcontextprotocol.io/v0/servers` is a live,
cursor-paginated JSON API (`nextCursor` for paging). Every entry is
already tagged with exactly the metadata Beacon needs to route and
pre-filter a server *before spending a probe cent*:

- **`remotes[]`** — a hosted `streamable-http` or `sse` URL, optionally
  with `headers` (auth). **These are the ones Beacon can probe today.**
- **`packages[]`** — npm / PyPI stdio packages with
  `environmentVariables` (credentials). **Not reachable over the network
  — stdio, Phase 6.**

This replaces the vaguer "MCP registry" reference in the first draft. The
in-session `SearchMcpRegistry` tool hits Claude's *connector directory*
(a curated, largely OAuth-gated commercial set — Supabase, Notion, Plaid,
DataHub, alphaXiv) — useful for big-name conformance cards, but **not**
the primary crawl source. Cross-check volume/liveness against **PulseMCP**
(~21k servers, has an API and a `?other[]=remote` filter), **Glama**
(~37k, official/claimed tiers), and **Smithery**.

### The sourcing funnel (the key finding)

Discovery scales to thousands; each filter narrows what we can actually
score, and how deeply:

```
  registry crawl        ~thousands   → every server: catalog metadata
  └ remotes[] (HTTP/SSE) subset      → Beacon can connect at all
    └ no required headers smaller    → FUNCTIONAL probe (dims 1–7, real A/B grades)
    └ headers required   the rest    → CONFORMANCE-ONLY (dims 1,2,5,7 + auth hygiene)
  packages[] (stdio)     large       → not scorable until Phase 6
```

Implication for expectations: **most discovered servers get a
conformance-only score, not a functional grade**, until either (a) tokens
are supplied for gated servers, or (b) stdio lands. This reframes Phase 6
from "optional nice-to-have" to *the unlock for the majority of the
ecosystem* — worth calling out to whoever prioritizes the roadmap.

### Curated seed (`targets_mcp.json`)

A handful with a deliberate spread of outcomes so the scoreboard
demonstrates every rung of the rubric:

- an authless remote with a safe read-only tool → earns a real **A**;
- a big-name OAuth remote (e.g. from the connector directory) → clean
  challenge → **conformance-only B**, proving auth hygiene ≠ outage;
- a deliberately-malicious fixture (injection in a tool description) →
  **F**, proving the security path.

The authless-remote seed entries are *generated*, not hand-guessed: run
the registry filter (`remotes[]` present, no required `headers`) and take
the first N that complete `initialize` — same "verify reachable before
committing" discipline `competitor_intel.py`/`seed_real_probes.py`
already follow.

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
- **Phase 4 — Discovery + scheduled refresh.** Crawl the registry
  `/v0/servers` API, pre-filter on `remotes[]` / required `headers` to
  route each server to functional vs conformance-only *before* spending a
  probe, persist the catalog, and wire the MCP set into
  `probe-refresh.yml`.
- **Phase 5 — UI.** Transport badge, tool count, per-dimension breakdown
  on the scoreboard and probe console.
- **Phase 6 — stdio transport (the ecosystem unlock).** Local/stdio MCP
  servers (npm/PyPI packages) via sandboxed process spawning. Per the
  sourcing funnel this is where *most* of the registry becomes scorable —
  not a nice-to-have, just correctly sequenced after the HTTP path proves
  the rubric. Deferred because it's material new infra + security surface.

---

## 7. Decisions to confirm before Phase 1

1. **Transport scope** — start HTTP/SSE-only (recommended; fits the
   container) and defer stdio to Phase 6? Or is stdio in scope now?
   *Note the funnel: HTTP-only means most of the registry is
   conformance-only until Phase 6.*
2. **OAuth servers** — conformance-only by default (recommended), or
   supply tokens (via env) for a chosen set so gated servers get full
   functional scores? Given the sourcing funnel, this is the main lever
   for how many servers earn a *real* A/B grade vs. a conformance ceiling.
3. **Coverage target** — is the goal a broad conformance leaderboard
   (thousands, shallow) or a curated deeply-probed set (dozens, real
   grades)? The rubric supports both; it changes how aggressively Phase 4
   crawls.
4. **Deliverable shape** — land this as one PR per phase (recommended),
   or a single larger PR through Phase 3?

Once these are settled, Phase 1 is a self-contained, low-risk change:
new `mcp_probe.py`, a `probe_type` branch in the engine, additive DB
columns, and a seed script — everything else in Beacon stays untouched.

---

## 8. Related work & network positioning (forward-looking, not in scope above)

The longer-term ambition behind this plan is a **shared, multi-host trust
network** — anyone runs the code, anyone contributes probes/attestations,
and the reputation mapping covers not just MCP servers and APIs but
**agents** too. That's a materially bigger project than Phases 1–6, so
it's captured here as positioning, not committed work.

**This is not a green field.** By mid-2026 several projects occupy
adjacent ground:

- **[ERC-8004 "Trustless Agents"](https://github.com/sudeepb02/awesome-erc8004)**
  — an Ethereum standard with three open, permissionless registries:
  Identity, Reputation, and **Validation** (third-party attestations of
  performance). Beacon's probe→attestation plane is structurally a
  Validation Registry operator.
- **[MIT Project NANDA](https://thenewstack.io/how-mits-project-nanda-aims-to-decentralize-ai-agents/)**
  — federated agent registries with no central directory, built on MCP +
  A2A, with its own Reputation Registry (public feedback) and Validation
  Registry (independent attestations). The "anyone hosts a node" property
  this plan wants, already specified for agents.
- **[OpenRank / EigenTrust](https://docs.openrank.com/openrank/ranking-and-reputation)**
  — verifiable reputation *compute* (EigenTrust graph algorithm, Sybil
  detection, EigenLayer-secured) as shared infrastructure, run over
  ERC-8004 data. This is the decentralized analog of `reputation.py`:
  anyone can recompute the index over open data and verify it
  cryptographically, rather than trusting one host's SQLite.
- **MCP-specific competitors**: Dominion Observatory (behavioral trust
  scores over 14,820+ MCP servers), AgentGraph (security-scan trust
  scores + W3C DIDs), cheqd Trust Registries (verifiable-credential
  model). Worth a direct look before Phase 1, since they score the exact
  same target class this plan does.
- **Payments/reputation coupling**: x402 (HTTP-402 micropayments) + AP2 +
  ERC-8004 are converging into a stack where verifiable payment history
  *is* a reputation signal — the same idea as Beacon's escrow gate
  consuming the reputation index, generalized to a network.

**Where Beacon is actually differentiated:** most of the above are either
agent-only (ERC-8004, NANDA, OpenRank) or MCP-only (Dominion, AgentGraph),
and most reputation registries are *passive* — they store feedback/
attestations that someone else generated. Beacon's edge is (a) **active
probing** — it generates ground truth by actually calling the target
rather than waiting for others to submit feedback, and (b) **one index
across MCPs, APIs, and (eventually) agents**, where most of the field
splits those into separate systems.

**Recommended framing, not a new phase:** don't build a new decentralized
substrate from scratch. Position Beacon as a **validation-layer node**
that *emits* attestations in an ERC-8004/EAS-compatible shape, so any
future multi-host network (NANDA-style federation, or a bespoke one) can
consume Beacon's probes without Beacon having to solve consensus, Sybil
resistance, or cross-host scoring agreement itself — those are exactly
the hard problems OpenRank/EigenTrust exist to solve, and re-deriving
them in-house would be the highest-risk part of "make this a true
network." A rated-agents dimension, if pursued, is its own follow-on plan
(agents need a different probe entirely — evaluating multi-turn/tool-use
behavior, not a single request/response), not an extension of the MCP
probe described above.
