#!/usr/bin/env python3
"""
verify_soul_config.py — Check whether SOUL.md has Memory OS Ground Truth Hierarchy.
Exits 0 if all checks pass, 1 if any are missing.
"""

import sys
from pathlib import Path

SOUL_PATH = Path.home() / ".hermes" / "SOUL.md"
MEMORY_OS_MARKER = "<!-- Memory OS additions — do not duplicate -->"

CHECKS = {
    "ground_truth": {
        "label": "Ground Truth Hierarchy (layer 7)",
        "marker": MEMORY_OS_MARKER,
        "keywords": [
            "Injected memory",
            "[qdrant]",
            "[fabric]",
            "[sessions]",
            "[facts]",
            "priority level 2",
        ],
        "fix_hint": (
            "Add the Ground Truth hierarchy from "
            "modifications/soul-rulebook.md to ~/.hermes/SOUL.md"
        ),
    },
    "context_injection": {
        "label": "Context Injection Convention",
        "marker": MEMORY_OS_MARKER,
        "keywords": [
            "Context Injection Convention",
            "treat it as prior knowledge",
            "use directly when reasoning",
        ],
        "fix_hint": (
            "Add the Context Injection Convention from "
            "modifications/soul-rulebook.md to ~/.hermes/SOUL.md"
        ),
    },
    "fact_feedback": {
        "label": "Fact Feedback Rule",
        "marker": MEMORY_OS_MARKER,
        "keywords": [
            "fact_feedback",
            "trust scoring system",
        ],
        "fix_hint": (
            "Add the Fact Feedback Rule from "
            "modifications/soul-rulebook.md to ~/.hermes/SOUL.md"
        ),
    },
    "honcho_deprecation": {
        "label": "Honcho Deprecation Notice",
        "marker": MEMORY_OS_MARKER,
        "keywords": [
            "**Deprecated:**",
            "abandoned as external memory",
        ],
        "fix_hint": (
            "Add the Honcho deprecation notice from "
            "modifications/soul-rulebook.md to ~/.hermes/SOUL.md"
        ),
    },
}


def main() -> int:
    if not SOUL_PATH.exists():
        print(f"❌ SOUL.md not found at {SOUL_PATH}")
        print(f"   Run 'hermes setup' first, then apply modifications/soul-rulebook.md")
        return 1

    content = SOUL_PATH.read_text("utf-8")
    marker_count = content.count(MEMORY_OS_MARKER)
    has_any_marker = marker_count > 0

    if not has_any_marker:
        print("ℹ️  SOUL.md lacks Memory OS markers (content may have been added manually)")
        print("   Proceeding with keyword checks...\n")

    all_ok = True
    for key, check in CHECKS.items():
        keywords_found = all(kw.lower() in content.lower() for kw in check["keywords"])

        if keywords_found:
            print(f"✅ {check['label']}")
        else:
            print(f"❌ {check['label']} — missing or incomplete")
            print(f"   {check['fix_hint']}")
            all_ok = False

    if all_ok:
        print(f"\n✅ All Memory OS SOUL.md checks pass ({len(CHECKS)}/{len(CHECKS)})")
        return 0
    else:
        missing = sum(1 for k, c in CHECKS.items()
                      if not all(kw.lower() in content.lower() for kw in c["keywords"]))
        print(f"\n⚠️  {missing}/{len(CHECKS)} checks failed.")
        print(f"   Apply missing sections from modifications/soul-rulebook.md")
        return 1


if __name__ == "__main__":
    sys.exit(main())
