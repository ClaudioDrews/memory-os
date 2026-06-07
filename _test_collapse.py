"""Test non-bijunctive recall collapse (Elyan Edition)."""
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from icarus.collapse import (
    tokenize, salience, score_all, collapse, DEFAULTS,
    physical_entropy, attest, verify_attestation,
)

all_ok = True


def check(name, cond):
    global all_ok
    if not cond:
        print(f"FAIL: {name}")
        all_ok = False


# ── tokenize ──
check("tokenize strips stopwords", tokenize("the quick brown fox") == {"quick", "brown", "fox"})
check("tokenize empty -> empty set", tokenize("") == set())
check("tokenize lowercases", tokenize("RustChain POWER8") == {"rustchain", "power8"})

# ── salience monotonic with overlap ──
q = tokenize("rustchain ed25519 attestation signature")
hi = salience({"text": "rustchain ed25519 attestation signature node", "source": "facts"}, q)
lo = salience({"text": "unrelated gardening tomatoes weather", "source": "facts"}, q)
check("salience rewards overlap", hi > lo)

# qdrant score lifts a candidate with no overlap above a zero-score one
sc_hi = salience({"text": "zzz none", "source": "qdrant", "score": 0.9}, q)
sc_lo = salience({"text": "zzz none", "source": "qdrant", "score": 0.1}, q)
check("salience rewards score", sc_hi > sc_lo)

# rank decay: later rank => lower salience, all else equal
r0 = salience({"text": "rustchain ed25519", "source": "fabric", "rank": 0}, q)
r3 = salience({"text": "rustchain ed25519", "source": "fabric", "rank": 3}, q)
check("rank decay lowers later ranks", r0 > r3)

# ── collapse: prune weak relative to strong ──
cands = [
    {"key": "strong", "source": "facts", "text": "rustchain ed25519 attestation signature verified node", "rank": 0},
    {"key": "mid", "source": "sessions", "text": "rustchain notes about something", "rank": 0},
    {"key": "weak", "source": "qdrant", "text": "completely unrelated gardening tomatoes", "score": 0.0, "rank": 0},
]
out = collapse(cands, q, budget=6, prune_ratio=0.35)
keys = [c["key"] for c in out]
check("strong survives", "strong" in keys)
check("weak pruned relative to strong", "weak" not in keys)
check("survivors carry _salience", all("_salience" in c for c in out))
check("survivors sorted strongest-first", out == sorted(out, key=lambda c: c["_salience"], reverse=True))

# ── collapse: budget cap ──
many = [
    {"key": f"k{i}", "source": "facts", "text": f"rustchain ed25519 attestation node {i}", "rank": 0}
    for i in range(20)
]
out2 = collapse(many, q, budget=4)
check("budget caps survivors", len(out2) <= 4)

# ── collapse: near-duplicate suppression ──
dups = [
    {"key": "a", "source": "facts", "text": "rustchain ed25519 attestation signature verified", "rank": 0},
    {"key": "b", "source": "qdrant", "text": "rustchain ed25519 attestation signature verified", "score": 0.9, "rank": 0},
    {"key": "c", "source": "sessions", "text": "totally different power8 numa coffer topic entirely", "rank": 0},
]
out3 = collapse(dups, tokenize("rustchain ed25519 attestation signature power8 numa"), budget=6, dup_overlap=0.82)
ids = [c["key"] for c in out3]
check("near-duplicate suppressed (a or b, not both)", not ("a" in ids and "b" in ids))

# ── edge cases ──
check("empty input -> []", collapse([], q) == [])
check("zero budget -> []", collapse(cands, q, budget=0) == [])
mixed = collapse([None, "x", 42, {"key": "ok", "source": "facts", "text": "rustchain ed25519 attestation"}], q)
check("non-dict items ignored (only the dict survives)", [c["key"] for c in mixed] == ["ok"])

# no query tokens: must NOT collapse to empty when there was real signal
out4 = collapse(cands, set(), budget=2)
check("empty query still returns survivors (no firehose, no blackout)", 0 < len(out4) <= 2)

# DEFAULTS sanity
check("DEFAULTS present", {"budget", "prune_ratio", "dup_overlap"} <= set(DEFAULTS))
check("DEFAULTS has amplify knobs", {"corroboration_overlap", "amplify_gain", "amplify_cap"} <= set(DEFAULTS))

# ── Hebbian cross-source amplify ──
qh = tokenize("rustchain ed25519 attestation signature")
# Same fact from TWO different sources (fabric + qdrant) should amplify; a lone
# unrelated item should not. Corroboration counts cross-source only.
corro_set = [
    {"key": "fab", "source": "fabric", "text": "rustchain ed25519 attestation signature verified", "rank": 0},
    {"key": "qdr", "source": "qdrant", "text": "rustchain ed25519 attestation signature verified", "score": 0.5, "rank": 0},
    {"key": "lone", "source": "sessions", "text": "rustchain ed25519 attestation signature note", "rank": 0},
]
scored = {r["candidate"]["key"]: r for r in score_all(corro_set, qh)}
check("cross-source corroboration counted", scored["fab"]["corroboration"] >= 1)
check("corroboration amplifies salience above base", scored["fab"]["salience"] > scored["fab"]["base"])
# same-source duplicates do NOT corroborate (must be cross-source)
same_src = score_all([
    {"key": "f1", "source": "facts", "text": "rustchain ed25519 attestation", "rank": 0},
    {"key": "f2", "source": "facts", "text": "rustchain ed25519 attestation", "rank": 1},
], qh)
check("same-source agreement does NOT amplify", all(r["corroboration"] == 0 for r in same_src))
# survivors carry _corroboration
amp_out = collapse(corro_set, qh, budget=6)
check("survivors annotated with _corroboration", all("_corroboration" in c for c in amp_out))

# ── physical-entropy attestation ──
ent = bytes(range(16))  # injected => deterministic for the test
a1 = attest(amp_out, entropy=ent)
check("attestation has hash+nonce+algo", {"hash", "nonce", "count", "algo"} <= set(a1))
check("attestation algo is blake2b-256", a1["algo"] == "blake2b-256")
check("attestation verifies for unchanged survivors", verify_attestation(amp_out, a1) is True)
# tamper-evidence: drop a survivor => verification fails
check("attestation FAILS when survivor set tampered", verify_attestation(amp_out[:-1], a1) is False if len(amp_out) > 1 else True)
# order-independent commitment: shuffled survivors verify the same
check("attestation order-independent", verify_attestation(list(reversed(amp_out)), a1) is True)
# determinism: same survivors + same nonce => same hash
check("attestation deterministic under fixed nonce", attest(amp_out, entropy=ent)["hash"] == a1["hash"])
# physical entropy: live nonce is non-empty and (essentially always) varies
e_a, e_b = physical_entropy(16), physical_entropy(16)
check("physical_entropy returns requested length", len(e_a) == 16)
check("physical_entropy is live (two draws differ)", e_a != e_b)
# different selection => different commitment under same nonce
other = collapse([{"key": "z", "source": "facts", "text": "unrelated power8 numa coffer", "rank": 0}], tokenize("power8 numa"))
check("different selection => different hash", attest(other, entropy=ent)["hash"] != a1["hash"])

# default (LIVE physical-entropy) attest path round-trips — exercises the impure
# branch, not just the injected-entropy one.
live = attest(amp_out)
check("default attest path verifies round-trip", verify_attestation(amp_out, live) is True)
check("default attest carries a live nonce", len(live["nonce"]) > 0 and live["nonce"] != a1["nonce"])

# identity (not text/salience) is committed: two DISTINCT survivors with the
# SAME source+text+salience but different keys must NOT cross-verify.
twinA = [{"key": "A", "source": "facts", "text": "same text", "_salience": 0.5}]
twinB = [{"key": "B", "source": "facts", "text": "same text", "_salience": 0.5}]
attA = attest(twinA, entropy=ent)
check("same source/text/salience but different key => different commitment",
      verify_attestation(twinB, attA) is False)

# physical_entropy clamps oversized requests instead of raising (blake2b max 64)
check("physical_entropy clamps >64 without raising", 1 <= len(physical_entropy(200)) <= 64)

# ── adapter tests: hooks._apply_collapse (the hot-path wiring) ──
# Silence the fail-open WARNING+traceback that the intentional malformed-input
# test below triggers by design — keeps test output clean.
import logging as _logging
_logging.disable(_logging.CRITICAL)
from icarus import hooks as _hooks

# strong fabric + relevant session survive; irrelevant zero-score qdrant pruned
af, aq, asn, afc = _hooks._apply_collapse(
    "rustchain ed25519 attestation signature",
    [{"id": "f1", "summary": "rustchain ed25519 attestation signature verified"}],
    [{"id": "q1", "title": "gardening", "content_preview": "tomatoes weather unrelated", "score": 0.0}],
    [{"session_id": "s1", "title": "rustchain", "snippet": "ed25519 attestation work"}],
    ["power8 numa coffer unrelated topic"],
)
check("adapter: strong fabric survives", [e["id"] for e in af] == ["f1"])
check("adapter: weak zero-score qdrant pruned", aq == [])
check("adapter: returns four lists", all(isinstance(x, list) for x in (af, aq, asn, afc)))

# qdrant text now reads `content`/`body`, not just title+preview (Codex fix)
qtxt = _hooks._qdrant_text({"content": "rustchain ed25519 attestation node verified"})
check("adapter: _qdrant_text reads content field", "ed25519" in qtxt)

# fail-open: malformed inputs must return unchanged tuple, never raise
bad = _hooks._apply_collapse("q", [{"no": "text"}], [None], [], [])
check("adapter: fail-open returns 4-tuple", len(bad) == 4)

# safe env parser: garbage value falls back to default, never raises
check("adapter: _env_num bad value -> default", _hooks._env_num("X_NOPE_BAD", 6, int) == 6)

if all_ok:
    print("=== ALL COLLAPSE TESTS PASS ===")
    sys.exit(0)
else:
    print("=== COLLAPSE TESTS FAILED ===")
    sys.exit(1)
