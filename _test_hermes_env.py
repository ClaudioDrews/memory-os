"""Test profile-aware path resolution (scripts/hermes_env.py + icarus/state.py mirrors).

Covers the resolution logic behind the profile-isolated memory feature:
default (no HERMES_HOME) layout, modern (<root>/profiles/<name>) layout,
legacy (~/.hermes-<name>) layout, and the FABRIC_DIR override that lets
profiles deliberately share a fabric.
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "scripts"))

import hermes_env  # noqa: E402

all_ok = True


def check(name, cond):
    global all_ok
    if not cond:
        print(f"FAIL: {name}")
        all_ok = False


class env:
    """Context manager that patches os.environ for the block, then restores it."""

    def __init__(self, **kw):
        self.kw = kw
        self.saved = {}

    def __enter__(self):
        self.saved = {k: os.environ.get(k) for k in self.kw}
        for k, v in self.kw.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


HOME = os.path.expanduser("~")

# ── hermes_env.hermes_home() ─────────────────────────────────────────────

with env(HERMES_HOME=None):
    check("hermes_home: default falls back to ~/.hermes",
          str(hermes_env.hermes_home()) == f"{HOME}/.hermes")

with env(HERMES_HOME="/tmp/hh/profiles/coder"):
    check("hermes_home: honors HERMES_HOME when set",
          str(hermes_env.hermes_home()) == "/tmp/hh/profiles/coder")

with env(HERMES_HOME="  "):
    check("hermes_home: blank/whitespace HERMES_HOME treated as unset",
          str(hermes_env.hermes_home()) == f"{HOME}/.hermes")

with env(HERMES_HOME="~/.hermes/profiles/coder"):
    check("hermes_home: expands ~ in HERMES_HOME",
          str(hermes_env.hermes_home()) == f"{HOME}/.hermes/profiles/coder")

# ── hermes_env.profile_name() / is_profile() ─────────────────────────────

with env(HERMES_HOME=None):
    check("profile_name: default profile is ''", hermes_env.profile_name() == "")
    check("is_profile: False for default profile", hermes_env.is_profile() is False)

with env(HERMES_HOME="/home/u/.hermes/profiles/coder"):
    check("profile_name: modern layout <root>/profiles/<name>",
          hermes_env.profile_name() == "coder")
    check("is_profile: True for modern profile layout", hermes_env.is_profile() is True)

with env(HERMES_HOME="/home/u/.hermes-reviewer"):
    check("profile_name: legacy .hermes-<name> layout",
          hermes_env.profile_name() == "reviewer")
    check("is_profile: True for legacy profile layout", hermes_env.is_profile() is True)

with env(HERMES_HOME="/home/u/.hermes-reviewer/"):
    check("profile_name: legacy layout strips trailing slash",
          hermes_env.profile_name() == "reviewer")

with env(HERMES_HOME="/some/custom/hermes-dir"):
    check("profile_name: non-profile-shaped HERMES_HOME is treated as default ('')",
          hermes_env.profile_name() == "")
    check("is_profile: False for non-profile-shaped HERMES_HOME",
          hermes_env.is_profile() is False)

# ── hermes_env.fabric_dir() ───────────────────────────────────────────────

with env(HERMES_HOME=None, FABRIC_DIR=None):
    check("fabric_dir: default profile falls back to ~/fabric",
          str(hermes_env.fabric_dir()) == f"{HOME}/fabric")

with env(HERMES_HOME="/home/u/.hermes/profiles/coder", FABRIC_DIR=None):
    check("fabric_dir: profile without override gets its own <home>/fabric",
          str(hermes_env.fabric_dir()) == "/home/u/.hermes/profiles/coder/fabric")

with env(HERMES_HOME="/home/u/.hermes/profiles/reviewer", FABRIC_DIR=None):
    check("fabric_dir: a second profile gets a *different* fabric than the first",
          str(hermes_env.fabric_dir()) != "/home/u/.hermes/profiles/coder/fabric")

with env(HERMES_HOME="/home/u/.hermes/profiles/coder", FABRIC_DIR="/shared/fabric"):
    check("fabric_dir: explicit FABRIC_DIR always wins over profile default",
          str(hermes_env.fabric_dir()) == "/shared/fabric")

with env(HERMES_HOME=None, FABRIC_DIR="/shared/fabric"):
    check("fabric_dir: explicit FABRIC_DIR wins for default profile too",
          str(hermes_env.fabric_dir()) == "/shared/fabric")

# ── hermes_env.*_db() / logs / wiki / dlq path composition ───────────────

with env(HERMES_HOME="/home/u/.hermes/profiles/coder"):
    hh = hermes_env.hermes_home()
    check("state_db: under profile home", hermes_env.state_db() == hh / "state.db")
    check("memory_store_db: under profile home",
          hermes_env.memory_store_db() == hh / "memory_store.db")
    check("logs_dir: under profile home", hermes_env.logs_dir() == hh / "logs")
    check("soul_path: under profile home", hermes_env.soul_path() == hh / "SOUL.md")
    check("wiki_state_file: under profile home",
          hermes_env.wiki_state_file() == hh / "wiki_ingest_state.json")
    check("wiki_failures_file: under profile home",
          hermes_env.wiki_failures_file() == hh / "wiki_ingest_failures.json")
    check("dlq_report_log: under profile home",
          hermes_env.dlq_report_log() == hh / "cron" / "output" / "dlq_reports.jsonl")
    check("dlq_report_dir: under profile home",
          hermes_env.dlq_report_dir() == hh / "cron" / "output" / "quality_report")
    check("query_telemetry_log: under profile home/logs",
          hermes_env.query_telemetry_log() == hh / "logs" / "query-telemetry.jsonl")
    check("reflection_log: under profile home/logs",
          hermes_env.reflection_log() == hh / "logs" / "reflection_trigger.log")


# ── icarus/state.py mirrors the same resolution (module-level constants,
#    so exercised via subprocess — each scenario needs a fresh import) ────

def state_py(env_overrides, expr):
    """Run `expr` against a freshly-imported icarus.state under env_overrides."""
    child_env = dict(os.environ)
    for k, v in env_overrides.items():
        if v is None:
            child_env.pop(k, None)
        else:
            child_env[k] = v
    proc = subprocess.run(
        [sys.executable, "-c", f"import icarus.state as s; print({expr})"],
        cwd=os.path.dirname(__file__) or ".",
        env=child_env,
        capture_output=True,
        text=True,
    )
    check(f"state.py subprocess ok ({expr} under {env_overrides})", proc.returncode == 0)
    return proc.stdout.strip()


check("icarus.state: default FABRIC_DIR == ~/fabric",
      state_py({"HERMES_HOME": None}, "s.FABRIC_DIR") == f"{HOME}/fabric")

check("icarus.state: profile FABRIC_DIR == <home>/fabric",
      state_py({"HERMES_HOME": "/tmp/hh/profiles/coder"}, "s.FABRIC_DIR")
      == "/tmp/hh/profiles/coder/fabric")

check("icarus.state: profile AGENT_NAME auto-detected from HERMES_HOME",
      state_py({"HERMES_HOME": "/tmp/hh/profiles/coder", "HERMES_AGENT_NAME": None},
                "s.AGENT_NAME") == "coder")

check("icarus.state: explicit HERMES_AGENT_NAME wins over auto-detection",
      state_py({"HERMES_HOME": "/tmp/hh/profiles/coder", "HERMES_AGENT_NAME": "custom"},
                "s.AGENT_NAME") == "custom")

check("icarus.state: legacy .hermes-<name> AGENT_NAME auto-detected",
      state_py({"HERMES_HOME": "/home/u/.hermes-reviewer", "HERMES_AGENT_NAME": None},
                "s.AGENT_NAME") == "reviewer")

with env(HERMES_HOME="/tmp/hh/profiles/coder", FABRIC_DIR=None):
    check("icarus.state and hermes_env agree on FABRIC_DIR for the same profile",
          state_py({"HERMES_HOME": "/tmp/hh/profiles/coder", "FABRIC_DIR": None},
                    "s.FABRIC_DIR") == str(hermes_env.fabric_dir()))

if all_ok:
    print("=== ALL HERMES_ENV TESTS PASS ===")
    sys.exit(0)
else:
    print("=== HERMES_ENV TESTS FAILED ===")
    sys.exit(1)
