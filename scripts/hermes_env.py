"""Hermes home resolution for Memory OS scripts.

Hermes Agent supports multiple profiles.  When a non-default profile is
active, Hermes sets ``HERMES_HOME`` to ``<root>/profiles/<name>`` (e.g.
``~/.hermes/profiles/fpvdeals``).  Every script and module in Memory OS must
resolve Hermes-owned paths through :func:`hermes_home` so that separate
profiles get separate state databases, fabric directories, logs, and wiki
state — instead of all profiles stomping on the default profile's files.

Resolution order:

1. ``HERMES_HOME`` environment variable (set by Hermes for the active
   profile, and propagated into cron subprocess environments).
2. ``~/.hermes`` (default profile / standalone usage).
"""

import os
from pathlib import Path


def hermes_home() -> Path:
    """Return the active Hermes home directory (profile-aware)."""
    env = os.environ.get("HERMES_HOME", "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".hermes"


def profile_name() -> str:
    """Return the active profile name, or \"\" for the default profile.

    Handles both modern (``<root>/profiles/<name>``) and legacy
    (``~/.hermes-<name>``) layouts.
    """
    home = hermes_home()
    if home.parent.name == "profiles":
        return home.name
    text = str(home)
    if ".hermes-" in text:
        return text.split(".hermes-")[-1].rstrip("/")
    return ""


def is_profile() -> bool:
    """True when running under a non-default Hermes profile."""
    return bool(profile_name())


def fabric_dir() -> Path:
    """Return the fabric (cross-session memory) directory for this profile.

    An explicit ``FABRIC_DIR`` env var always wins (this is how you share a
    fabric between profiles deliberately).  Otherwise each profile gets its
    own fabric under its Hermes home, and the default profile keeps the
    historical ``~/fabric`` location for backwards compatibility.
    """
    env = os.environ.get("FABRIC_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    if is_profile():
        return hermes_home() / "fabric"
    return Path.home() / "fabric"


def state_db() -> Path:
    """Return the profile's session database (state.db)."""
    return hermes_home() / "state.db"


def memory_store_db() -> Path:
    """Return the profile's fact store database (memory_store.db)."""
    return hermes_home() / "memory_store.db"


def logs_dir() -> Path:
    """Return the profile's log directory."""
    return hermes_home() / "logs"


def soul_path() -> Path:
    """Return the profile's SOUL.md path."""
    return hermes_home() / "SOUL.md"


def wiki_state_file() -> Path:
    """Return the profile's wiki ingestion state file."""
    return hermes_home() / "wiki_ingest_state.json"


def wiki_failures_file() -> Path:
    """Return the profile's wiki DLQ (dead-letter queue) file."""
    return hermes_home() / "wiki_ingest_failures.json"


def dlq_report_log() -> Path:
    """Return the profile's DLQ report log."""
    return hermes_home() / "cron" / "output" / "dlq_reports.jsonl"


def dlq_report_dir() -> Path:
    """Return the profile's DLQ report output directory."""
    return hermes_home() / "cron" / "output" / "quality_report"


def query_telemetry_log() -> Path:
    """Return the profile's query telemetry log."""
    return logs_dir() / "query-telemetry.jsonl"


def reflection_log() -> Path:
    """Return the profile's reflection trigger log."""
    return logs_dir() / "reflection_trigger.log"
