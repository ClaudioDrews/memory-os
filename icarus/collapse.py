"""Non-bijunctive recall collapse.

Stock recall pulls a fixed quota from each memory source (fabric, qdrant,
sessions, facts) and injects all of it. A *strong* session memory and a *weak*
vector hit both survive because they live in separate per-source buckets.

This module unifies every candidate into one salience-ranked pool and applies a
Hebbian-style collapse borrowed (in structure only) from the PSE doctrine:

  - PRUNE   weak paths *relative to the strongest* (not an absolute floor) —
            noise doesn't vote.
  - AMPLIFY strong paths. Two senses: (a) the highest-salience candidates fill
            the budget, and (b) HEBBIAN CROSS-SOURCE CORROBORATION — when the
            same fact surfaces from 2+ *different* sources, that co-activation
            ("fire together, wire together") boosts its salience. Agreement
            across layers is evidence, so it amplifies.
  - BUDGET  spend ONE cross-source budget — the best N things get injected,
            regardless of which layer produced them.

ATTESTATION (RustChain doctrine tie-in): every collapse can emit a
physical-entropy hash attestation over its survivor set — a blake2b commitment
(same family as the RustChain Ergo anchor) bound to a hardware-seeded entropy
nonce. This makes a recall decision *tamper-evident* (you can verify which
memories were chosen) and *proof-of-live* (the entropy nonce proves a fresh
selection, not a replayed/emulated one). It is the recall analogue of RustChain's
anti-emulation fingerprinting, and it turns the collapse from an unobservable
black box into an auditable one.

``collapse``/``score_all``/``salience``/``tokenize``/``attest`` are pure (no I/O,
no globals) when given their inputs; only ``physical_entropy`` touches the
machine. Callers treat any collapse exception as "inject everything, unchanged."
Tunables are passed explicitly so behavior is fully deterministic for tests.
"""

from __future__ import annotations

import hashlib
import os
import time
from typing import Iterable

__all__ = [
    "tokenize", "salience", "score_all", "collapse", "DEFAULTS",
    "physical_entropy", "attest", "verify_attestation",
]

import re

# Mild per-source priors. Curated/durable sources get a small nudge; this only
# breaks ties between candidates of otherwise-equal salience. Kept close to 1.0
# on purpose — query relevance should dominate, not source identity.
_SOURCE_PRIOR = {
    "facts": 1.10,     # durable, hand-curated facts about the world
    "fabric": 1.05,    # cross-session decisions/resolutions
    "sessions": 1.00,  # prior conversation snippets
    "qdrant": 1.00,    # vector knowledge base
}

DEFAULTS = {
    "budget": 6,             # max candidates injected across ALL sources
    "prune_ratio": 0.35,     # keep candidates with salience >= ratio * max_salience
    "dup_overlap": 0.82,     # token-overlap above this vs a kept survivor => drop
    "overlap_weight": 0.55,  # weight of query-overlap vs base score in salience
    "rank_decay": 0.85,      # geometric decay applied per within-source rank
    # Hebbian cross-source amplify:
    "corroboration_overlap": 0.50,  # cross-source token-overlap that counts as agreement
    "amplify_gain": 0.15,    # salience boost per corroborating other-source candidate
    "amplify_cap": 0.50,     # max total boost fraction (caps runaway amplification)
}

_STOPWORDS = frozenset(
    "the a an is was are to of in for on with it and or not i you can do this "
    "that what how please help me my your we our they them then than over such "
    "be been being have has had will would could should about into only also "
    "just like very from at as by if".split()
)


def tokenize(text: str) -> set:
    """Lowercase alphanumeric tokens, minus stopwords. Pure and deterministic."""
    if not text:
        return set()
    words = set(re.findall(r"[a-z0-9]+", str(text).lower()))
    return words - _STOPWORDS


def _clamp01(x: float) -> float:
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


def _overlap(a: set, b: set) -> float:
    """Containment overlap: |a∩b| / min(|a|,|b|). 0 if either is empty."""
    if not a or not b:
        return 0.0
    return len(a & b) / (min(len(a), len(b)) or 1)


def salience(candidate: dict, query_tokens: set, *,
             overlap_weight: float = DEFAULTS["overlap_weight"],
             rank_decay: float = DEFAULTS["rank_decay"]) -> float:
    """Unified base salience for one candidate, in [0, ~1.2] (pre-amplify).

    Combines query-token overlap, base score (qdrant cosine when present;
    neutral prior otherwise), within-source rank decay, and a mild per-source
    prior. A candidate dict may carry: ``text``, ``score`` (float|None),
    ``rank`` (int, 0-based within its source), ``source``.
    """
    text_tokens = tokenize(candidate.get("text", ""))
    overlap = (len(query_tokens & text_tokens) / len(query_tokens)) if query_tokens else 0.0

    score = candidate.get("score")
    base = _clamp01(float(score)) if score is not None else 0.6

    sw = _clamp01(overlap_weight)
    blended = sw * overlap + (1.0 - sw) * base

    rank = int(candidate.get("rank", 0) or 0)
    decay = rank_decay ** max(rank, 0)

    prior = _SOURCE_PRIOR.get(candidate.get("source", ""), 1.0)
    return blended * decay * prior


def score_all(candidates: Iterable[dict], query_tokens: set, *,
              overlap_weight: float = DEFAULTS["overlap_weight"],
              rank_decay: float = DEFAULTS["rank_decay"],
              corroboration_overlap: float = DEFAULTS["corroboration_overlap"],
              amplify_gain: float = DEFAULTS["amplify_gain"],
              amplify_cap: float = DEFAULTS["amplify_cap"]) -> list:
    """Score every candidate with base salience + Hebbian cross-source amplify.

    Returns a list of dicts (NOT sorted) — one per input dict — each with:
      ``base`` (pre-amplify salience), ``corroboration`` (count of OTHER-source
      candidates whose text agrees above ``corroboration_overlap``), ``salience``
      (base * (1 + min(corroboration*amplify_gain, amplify_cap))), and
      ``candidate`` (the original dict). Pure; used by collapse() and the
      debug/eval path so scores aren't recomputed.

    Cost: O(n²) in pool size from the cross-source corroboration scan. The pool
    is the per-turn recall candidate set (low dozens at most), so this is
    negligible on the hot path; it would matter only if budgets grew large.
    """
    pool = [c for c in candidates if isinstance(c, dict)]
    toks = [tokenize(c.get("text", "")) for c in pool]
    bases = [salience(c, query_tokens, overlap_weight=overlap_weight,
                      rank_decay=rank_decay) for c in pool]

    out = []
    for i, c in enumerate(pool):
        src = c.get("source")
        corro = 0
        if toks[i]:
            for j, c2 in enumerate(pool):
                if i == j or c2.get("source") == src:
                    continue  # Hebbian agreement is CROSS-source only
                if _overlap(toks[i], toks[j]) >= corroboration_overlap:
                    corro += 1
        # Attenuate corroboration boost by query-local relevance.
        # A globally-important fact that surfaces in many sources should not
        # receive full Hebbian amplification when the current query is only
        # tangentially related. The base salience already encodes query overlap;
        # using it as an attenuation factor ensures corroboration helps most
        # when the candidate is already query-relevant: fire together, wire
        # together — but only light the wire when the query is the spark.
        boost = min(corro * amplify_gain * bases[i], amplify_cap)
        out.append({
            "base": bases[i],
            "corroboration": corro,
            "salience": bases[i] * (1.0 + boost),
            "candidate": c,
        })
    return out


def collapse(candidates: Iterable[dict], query_tokens: set, *,
             budget: int = DEFAULTS["budget"],
             prune_ratio: float = DEFAULTS["prune_ratio"],
             dup_overlap: float = DEFAULTS["dup_overlap"],
             overlap_weight: float = DEFAULTS["overlap_weight"],
             rank_decay: float = DEFAULTS["rank_decay"],
             corroboration_overlap: float = DEFAULTS["corroboration_overlap"],
             amplify_gain: float = DEFAULTS["amplify_gain"],
             amplify_cap: float = DEFAULTS["amplify_cap"]) -> list:
    """Collapse a unified candidate pool to a salience-ranked survivor list.

    Returns the surviving candidate dicts, strongest first, each annotated with
    ``_salience`` (post-amplify) and ``_corroboration`` (cross-source agreement
    count). Length <= ``budget``.

    Non-bijunctive: weak paths are pruned relative to the strongest survivor,
    not against an absolute threshold. Hebbian: cross-source agreement amplifies
    salience so a fact two layers both surfaced outranks a lone strong hit.

    Empty input or non-positive budget returns ``[]``. Pure function.
    """
    if budget <= 0:
        return []
    scored = score_all(candidates, query_tokens,
                       overlap_weight=overlap_weight, rank_decay=rank_decay,
                       corroboration_overlap=corroboration_overlap,
                       amplify_gain=amplify_gain, amplify_cap=amplify_cap)
    if not scored:
        return []

    max_s = max((r["salience"] for r in scored), default=0.0)

    # PRUNE: relative floor. When max_s is 0 (no overlap, no scores) the floor is
    # 0 and nothing is pruned here — budget + rank ordering still bound output so
    # we never inject a firehose, and never collapse to empty given real signal.
    floor = max_s * prune_ratio
    kept = [r for r in scored if r["salience"] >= floor]

    # AMPLIFY (ranking sense): strongest first. Stable for equal salience.
    kept.sort(key=lambda r: r["salience"], reverse=True)

    # Near-duplicate suppression: drop a redundant copy of an already-kept
    # survivor. The kept representative already carries the corroboration boost,
    # so cross-source agreement strengthens the survivor rather than wasting a
    # budget slot on the twin.
    survivors: list = []
    survivor_tokens: list = []
    for r in kept:
        if len(survivors) >= budget:
            break
        ctoks = tokenize(r["candidate"].get("text", ""))
        if any(_overlap(ctoks, st) >= dup_overlap for st in survivor_tokens):
            continue
        annotated = dict(r["candidate"])
        annotated["_salience"] = round(r["salience"], 4)
        annotated["_corroboration"] = r["corroboration"]
        survivors.append(annotated)
        survivor_tokens.append(ctoks)

    return survivors


# ── Physical-entropy hash attestation (RustChain doctrine tie-in) ────────────
# A recall decision should be auditable the way a RustChain block is: bound to a
# hash, and proven live by hardware entropy. attest() commits to the survivor
# set; physical_entropy() supplies a nonce the way RustChain's miners draw on
# clock-skew/timebase jitter (mftb on POWER8) — anti-replay, anti-emulation.

def physical_entropy(nbytes: int = 16) -> bytes:
    """Gather a hardware-seeded entropy nonce. IMPURE (touches the machine).

    Mixes the kernel CSPRNG (``os.urandom`` — hardware-entropy seeded) with
    microarchitectural timer jitter (``perf_counter_ns`` low bits sampled in a
    tight loop — the same clock-skew family RustChain fingerprints with, and on
    POWER8 the natural home of the ``mftb`` timebase). The jitter component is
    what makes the nonce proof-of-live rather than merely random.
    """
    jitter = bytearray()
    last = time.perf_counter_ns()
    for _ in range(64):
        now = time.perf_counter_ns()
        jitter.append((now - last) & 0xFF)
        last = now
    seed = os.urandom(32) + bytes(jitter)
    # blake2b digest_size is bounded to [1, 64]; clamp so an over-large request
    # returns a (shorter) nonce instead of raising. (tri-brain Codex)
    n = max(1, min(int(nbytes), 64))
    return hashlib.blake2b(seed, digest_size=n).digest()


def _survivor_commitment(survivors) -> bytes:
    """Stable canonical bytes over the survivor IDENTITY set (order-independent).

    Identity = source + the candidate's ``key`` when present (the strongest,
    caller-assigned identity), else a digest of the text. Salience is
    deliberately EXCLUDED: it is derived metadata, not part of "which memories
    were selected", and a serialized float would make the commitment fragile
    across a JSON round-trip. Committing to identity alone makes the attestation
    both stronger (no source/text/salience collision can forge a match — Codex
    BLOCKING) and stable across serialization (no float repr — Grok). 2026-06-04.
    """
    rows = []
    for c in survivors:
        if not isinstance(c, dict):
            continue
        key = c.get("key")
        if key is not None:
            ident = str(key)
        else:
            text = str(c.get("text", ""))
            ident = hashlib.blake2b(text.encode("utf-8", "replace"), digest_size=8).hexdigest()
        rows.append(f"{c.get('source','')}:{ident}")
    rows.sort()  # order-independent commitment
    return "|".join(rows).encode("utf-8")


def attest(survivors, *, entropy: bytes | None = None, salt: bytes = b"") -> dict:
    """Produce a tamper-evident, proof-of-live attestation over ``survivors``.

    Pure when ``entropy`` is supplied (deterministic — for tests); otherwise it
    draws a fresh nonce from :func:`physical_entropy`. Returns a record:
      ``hash`` (blake2b-256 hex commitment), ``nonce`` (hex entropy nonce),
      ``count`` (survivor count), ``algo``. Verify later with
      :func:`verify_attestation`.
    """
    nonce = entropy if entropy is not None else physical_entropy(16)
    commit = _survivor_commitment(survivors)
    digest = hashlib.blake2b(commit + b"|" + nonce + b"|" + salt,
                             digest_size=32).hexdigest()
    return {
        "hash": digest,
        "nonce": nonce.hex(),
        "count": sum(1 for c in survivors if isinstance(c, dict)),
        "algo": "blake2b-256",
    }


def verify_attestation(survivors, attestation: dict, *, salt: bytes = b"") -> bool:
    """True iff ``survivors`` reproduce the committed hash under the recorded nonce.

    Tamper-evidence: any change to the selected set (add/drop/alter a survivor)
    breaks the hash. Pure.
    """
    try:
        nonce = bytes.fromhex(attestation["nonce"])
        commit = _survivor_commitment(survivors)
        expect = hashlib.blake2b(commit + b"|" + nonce + b"|" + salt,
                                 digest_size=32).hexdigest()
        return expect == attestation.get("hash")
    except (KeyError, ValueError, TypeError):
        return False
