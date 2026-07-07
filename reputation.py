"""Beacon reputation index — a 0–1000 credit-score-style trust index for an
API endpoint, *earned over its probe history* rather than set by a single call.

Every probe is stored as its own row in the `scores` table, so the history
already exists; this module aggregates it. Each row is one observation: a
per-probe *quality* in [0,100] (what `probe_engine.calculate_trust_score`
produced), taken over `sample_size` calls, at time `created_at`.

The index is a sum of three transparent components (max 1000):

  Quality (0–650)      recency-weighted mean quality, shrunk toward a neutral
                       prior by how much evidence exists — so a single lucky
                       probe lands mid-pack, not at the ceiling.
  Track record (0–200) log-scaled call volume, paid out only in proportion to
                       quality — sustained good history is what lifts an
                       endpoint toward the top. This is why the index does not
                       "max out": reaching it takes a real track record.
  Reliability (0–150)  weighted share of probes that didn't error, times
                       consistency (low quality variance).

A recent scam/honeypot signal hard-caps the index (fraud is not outweighed by
volume). Grades and the escrow threshold are *derived from the index* here, so
they can never drift out of sync with the number the way per-row frozen grades
did (a stored 100 graded "A" sitting next to a 99 graded "A+").
"""
import math
from datetime import datetime, timezone

INDEX_MAX = 1000
QUALITY_MAX = 650
TRACK_MAX = 200
RELIABILITY_MAX = 150

# Neutral prior the quality is shrunk toward, and how much evidence (in summed
# observation weight) it takes to overcome it. A lone fresh probe carries ~1
# unit of weight, so with an 8-unit prior it stays near the prior until a
# handful of probes accumulate.
PRIOR_QUALITY = 55.0
PRIOR_WEIGHT = 8.0

# Recency: an observation's influence decays with e^(-age/τ). ~45 days means a
# probe from ~1.5 months ago counts ~1/e as much as a fresh one, so recent
# failures pull the index down and stale successes stop propping it up.
RECENCY_TAU_DAYS = 45.0

# Call volume that earns essentially full track-record credit (log-scaled).
VOLUME_LOG_REF = math.log10(5000.0)

# A recent scam/honeypot detection caps the index here regardless of anything
# else, and only counts if it is recent (still meaningfully weighted).
SCAM_CAP = 130
SCAM_RECENCY_FLOOR = 0.25

# Escrow gate opens at / above this index; the RISKY band sits just below it.
GATE_THRESHOLD = 700
RISKY_THRESHOLD = 600

# Only the most recent N probes per endpoint feed the index. Recency decay
# already makes older probes negligible, and this bounds both the query and
# the aggregation as scheduled re-probes accumulate rows over time. Must be
# applied identically wherever history is read so probe_engine and ledger
# compute the same index.
HISTORY_LIMIT = 400


def grade_for_index(index: int) -> str:
    if index >= 900:
        return "A+"
    if index >= 800:
        return "A"
    if index >= 700:
        return "B"
    if index >= 600:
        return "C"
    if index >= 450:
        return "D"
    return "F"


def escrow_recommendation(index: int) -> str:
    if index >= GATE_THRESHOLD:
        return "PASS"
    if index >= RISKY_THRESHOLD:
        return "HOLD"
    return "BLOCK"


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _row_get(row, key, default=None):
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def compute_index(rows, now: datetime | None = None) -> dict:
    """Aggregate all probe rows for one endpoint into a reputation index.

    `rows` is any iterable of mappings with keys: trust_score (per-probe
    quality 0–100), sample_size, created_at, request_status, evaluator,
    scam_flag. Returns index/grade/scam plus a display breakdown.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    obs = []
    for r in rows:
        quality = _clamp(float(_row_get(r, "trust_score", 0) or 0), 0.0, 100.0)
        calls = max(1, int(_row_get(r, "sample_size", 1) or 1))
        ts = _parse_ts(_row_get(r, "created_at"))
        age_days = max(0.0, (now - ts).total_seconds() / 86400.0) if ts else 0.0
        recency = math.exp(-age_days / RECENCY_TAU_DAYS)
        status = _row_get(r, "request_status")
        evaluator = _row_get(r, "evaluator")
        is_error = status in ("error", "CRITICAL_FAILURE") or evaluator == "error-path"
        is_scam = bool(_row_get(r, "scam_flag"))
        dispute_rate = _clamp(float(_row_get(r, "dispute_rate", 0) or 0), 0.0, 1.0)
        # A row summarizing many calls carries more weight than a single probe,
        # but log-scaled so a huge synthetic sample can't utterly dominate.
        weight = recency * (1.0 + math.log10(calls))
        obs.append({
            "q": quality, "calls": calls, "recency": recency,
            "is_error": is_error, "is_scam": is_scam, "dispute_rate": dispute_rate, "w": weight,
        })

    if not obs:
        empty = [
            {"label": "Quality", "value": 0, "max": QUALITY_MAX},
            {"label": "Track record", "value": 0, "max": TRACK_MAX},
            {"label": "Reliability", "value": 0, "max": RELIABILITY_MAX},
        ]
        return {"index": 0, "grade": "F", "scam": False, "sample_calls": 0, "breakdown": empty}

    total_w = sum(o["w"] for o in obs) or 1e-9
    q_mean = sum(o["w"] * o["q"] for o in obs) / total_w
    variance = sum(o["w"] * (o["q"] - q_mean) ** 2 for o in obs) / total_w
    std = math.sqrt(variance)

    # Quality: shrink the weighted mean toward the prior by available evidence.
    q_shrunk = (PRIOR_QUALITY * PRIOR_WEIGHT + q_mean * total_w) / (PRIOR_WEIGHT + total_w)
    quality_pts = QUALITY_MAX * (q_shrunk / 100.0)

    # Track record: recency-weighted call volume, scaled by quality so volume
    # of *bad* calls earns nothing.
    eff_calls = sum(o["recency"] * o["calls"] for o in obs)
    vol_frac = _clamp(math.log10(1.0 + eff_calls) / VOLUME_LOG_REF, 0.0, 1.0)
    track_pts = TRACK_MAX * (q_shrunk / 100.0) * vol_frac

    # Reliability: share of non-error weight, dampened by quality inconsistency
    # and by how often callers dispute the endpoint's responses.
    nonerror_share = sum(o["w"] for o in obs if not o["is_error"]) / total_w
    consistency = _clamp(1.0 - std / 40.0, 0.0, 1.0)
    dispute_rate = sum(o["w"] * o["dispute_rate"] for o in obs) / total_w
    dispute_factor = _clamp(1.0 - dispute_rate * 4.0, 0.4, 1.0)
    reliability_pts = RELIABILITY_MAX * nonerror_share * consistency * dispute_factor

    index = quality_pts + track_pts + reliability_pts
    scam_recent = any(o["is_scam"] and o["recency"] > SCAM_RECENCY_FLOOR for o in obs)
    if scam_recent:
        index = min(index, float(SCAM_CAP))

    index = int(round(_clamp(index, 0.0, float(INDEX_MAX))))

    if scam_recent:
        # A capped fraud verdict: show the whole (low) index as a quality
        # penalty rather than pretending track record/reliability earned points.
        breakdown = [
            {"label": "Quality", "value": index, "max": QUALITY_MAX},
            {"label": "Track record", "value": 0, "max": TRACK_MAX},
            {"label": "Reliability", "value": 0, "max": RELIABILITY_MAX},
        ]
    else:
        breakdown = [
            {"label": "Quality", "value": int(round(quality_pts)), "max": QUALITY_MAX},
            {"label": "Track record", "value": int(round(track_pts)), "max": TRACK_MAX},
            {"label": "Reliability", "value": int(round(reliability_pts)), "max": RELIABILITY_MAX},
        ]

    return {
        "index": index,
        "grade": grade_for_index(index),
        "scam": scam_recent,
        "sample_calls": int(sum(o["calls"] for o in obs)),
        "breakdown": breakdown,
    }
