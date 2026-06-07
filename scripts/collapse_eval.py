#!/usr/bin/env python3
"""collapse_eval.py — stock vs non-bijunctive collapse, with numbers.

Shows, on a sample multi-source candidate pool, what STOCK Memory OS would
inject (every per-source quota) versus what the Elyan Edition COLLAPSE injects
(one salience-ranked, Hebbian-amplified, deduplicated budget) — plus a rough
token estimate of the savings and a physical-entropy attestation over the
selected set.

Run: python3 scripts/collapse_eval.py
No deps beyond icarus.collapse. Deterministic except the live attestation nonce.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from icarus import collapse as C  # noqa: E402

# A realistic pool: the same query hits four sources. Several results restate
# the same fact (cross-source agreement) and several are weak/off-topic.
QUERY = "how does rustchain prevent VM farms from gaming rewards"
POOL = [
    # source, text, score(qdrant only)
    ("fabric",   "RIP-PoA hardware fingerprint: 6 checks (clock skew, cache, SIMD, thermal, jitter, anti-emulation) must all pass for RTC reward", None),
    ("fabric",   "Discussed minecraft RTC reward rates for diamonds and bosses", None),
    ("qdrant",   "Anti-emulation check flags QEMU/KVM; VMs earn ~1e-9 weight by design to stop VM farms", 0.71),
    ("qdrant",   "RustChain block time is 600s, epoch 144 blocks", 0.44),
    ("qdrant",   "Hardware fingerprint: clock-skew + cache-timing + anti-emulation gate rewards; VMs get near-zero weight", 0.66),
    ("sessions", "Earlier we confirmed VM fingerprint detection assigns 1 billionth weight to QEMU guests — anti VM-farm by design", None),
    ("sessions", "Talked about the Halo CE server on Windows", None),
    ("facts",    "VM farms are defeated by the anti-emulation fingerprint check: hypervisor detection -> 0.000000001x weight", None),
    ("facts",    "User prefers Python for bridge scripts", None),
]


def estimate_tokens(text: str) -> int:
    # ~4 chars/token rough heuristic — good enough for a relative comparison.
    return max(1, len(text) // 4)


def to_candidates(pool):
    by_source = {}
    cands = []
    for src, text, score in pool:
        rank = by_source.get(src, 0)
        by_source[src] = rank + 1
        cands.append({"key": (src, rank), "source": src, "text": text,
                      "score": score, "rank": rank})
    return cands


def main():
    cands = to_candidates(POOL)
    qtokens = C.tokenize(QUERY)

    print(f"Query: {QUERY!r}\n")
    print(f"STOCK (emit every source's quota): {len(cands)} memories")
    stock_tokens = sum(estimate_tokens(c["text"]) for c in cands)
    print(f"  ~{stock_tokens} tokens injected\n")

    survivors = C.collapse(cands, qtokens)
    print(f"COLLAPSE (one salience budget, Hebbian-amplified): {len(survivors)} memories")
    for s in survivors:
        print(f"  sal={s['_salience']:.3f} corro={s['_corroboration']}  "
              f"[{s['source']}] {s['text'][:60]}")
    collapse_tokens = sum(estimate_tokens(s["text"]) for s in survivors)
    print(f"  ~{collapse_tokens} tokens injected")

    if stock_tokens:
        saved = 100 * (1 - collapse_tokens / stock_tokens)
        print(f"\nToken reduction: {stock_tokens} -> {collapse_tokens}  ({saved:.0f}% fewer)")
    pruned = len(cands) - len(survivors)
    print(f"Pruned {pruned} weak/off-topic/duplicate memories; "
          f"kept the cross-source-corroborated signal.")

    att = C.attest(survivors)
    print(f"\nAttestation (tamper-evident, proof-of-live):")
    print(f"  hash  : {att['hash']}")
    print(f"  nonce : {att['nonce']}  ({att['algo']}, {att['count']} survivors)")
    print(f"  verify: {C.verify_attestation(survivors, att)}")


if __name__ == "__main__":
    main()
