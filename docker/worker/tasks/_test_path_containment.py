"""Test path-containment check in ingest_file rejects sibling-prefix paths.

Regression test for the `startswith` containment bug: when WIKI_PATH was
"/wiki", a file_path of "/wiki-shared/x.md" string-prefix-matched and bypassed
the check, even though "/wiki-shared" is a sibling of "/wiki", not a child.

Run from repo root:
    python3 docker/worker/tasks/_test_path_containment.py
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

# Make services/ importable from the worker dir so tasks.file_ingestion loads.
HERE = Path(__file__).resolve()
WORKER_DIR = HERE.parent.parent  # docker/worker
sys.path.insert(0, str(WORKER_DIR))

# Stub the embedding services before importing the module under test —
# they pull network deps we don't have in the test env.
import types
sparse_stub = types.ModuleType("services.sparse_embedding")
sparse_stub.get_sparse_embedding = lambda text: ([], [])
embed_stub = types.ModuleType("services.embedding")
async def _fake_embed(text):
    return [0.0] * 4
embed_stub.get_embedding = _fake_embed
services_pkg = types.ModuleType("services")
sys.modules["services"] = services_pkg
sys.modules["services.sparse_embedding"] = sparse_stub
sys.modules["services.embedding"] = embed_stub

# Likewise stub qdrant_client so the module loads without the dep.
qdrant_pkg = types.ModuleType("qdrant_client")
qdrant_models = types.ModuleType("qdrant_client.models")
class _PointStruct:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
qdrant_models.PointStruct = _PointStruct
class _AsyncQdrantClient:  # pragma: no cover - placeholder
    pass
qdrant_pkg.AsyncQdrantClient = _AsyncQdrantClient
qdrant_pkg.models = qdrant_models
sys.modules["qdrant_client"] = qdrant_pkg
sys.modules["qdrant_client.models"] = qdrant_models


def main():
    failures = 0

    with tempfile.TemporaryDirectory() as parent:
        # Create sibling dirs sharing a string prefix: "wiki" vs "wiki-shared".
        wiki_root = Path(parent) / "wiki"
        sibling = Path(parent) / "wiki-shared"
        wiki_root.mkdir()
        sibling.mkdir()

        inside_ok = wiki_root / "notes.md"
        inside_ok.write_text("hello")
        sibling_md = sibling / "secret.md"
        sibling_md.write_text("not allowed")

        os.environ["WIKI_PATH"] = str(wiki_root)
        os.environ["COLLECTION_NAME"] = "test"

        # Import here so WIKI_PATH is in env before module-level read.
        from tasks import file_ingestion
        # Force the module's cached WIKI_PATH to our test root.
        file_ingestion.WIKI_PATH = str(wiki_root)

        class _FakeQdrant:
            async def upsert(self, **kw):
                return None
            async def set_payload(self, **kw):
                return None

        loop = asyncio.new_event_loop()
        try:
            # 1. Sibling-prefix path must be rejected before the file is read.
            # With the old `startswith` check, `/wiki-shared/x.md` slipped past
            # the entry guard, the file body was read into memory, and only the
            # downstream get_source_tag() call raised — a regression that lets
            # an attacker with ARQ enqueue access trigger arbitrary .md reads.
            try:
                loop.run_until_complete(
                    file_ingestion.ingest_file(_FakeQdrant(), str(sibling_md))
                )
                print(f"FAIL: sibling-prefix path was accepted: {sibling_md}")
                failures += 1
            except ValueError as e:
                # Must be the entry-guard message, not the downstream
                # relative_to() leak from get_source_tag().
                if str(e) != "file_path must be a .md file under WIKI_PATH":
                    print(f"FAIL: containment bypassed; error came from "
                          f"downstream check, not entry guard: {e!r}")
                    failures += 1

            # 2. Real child path must still be accepted (smoke).
            try:
                # Don't actually run the full pipeline — stub upsert_with_dedup
                # so the containment check is what's exercised.
                async def _fake_dedup(**kw):
                    return {"status": "upserted", "id": "ok"}
                file_ingestion.upsert_with_dedup = _fake_dedup
                result = loop.run_until_complete(
                    file_ingestion.ingest_file(_FakeQdrant(), str(inside_ok))
                )
                if result.get("status") != "upserted":
                    print(f"FAIL: valid child path returned {result!r}")
                    failures += 1
            except ValueError as e:
                print(f"FAIL: valid child path rejected: {e!r}")
                failures += 1

            # 3. Non-.md file under wiki_root must be rejected.
            non_md = wiki_root / "evil.txt"
            non_md.write_text("nope")
            try:
                loop.run_until_complete(
                    file_ingestion.ingest_file(_FakeQdrant(), str(non_md))
                )
                print(f"FAIL: non-.md path was accepted: {non_md}")
                failures += 1
            except ValueError:
                pass
        finally:
            loop.close()

    if failures == 0:
        print("=== ALL PATH-CONTAINMENT TESTS PASS ===")
        sys.exit(0)
    else:
        print(f"=== {failures} PATH-CONTAINMENT TESTS FAILED ===")
        sys.exit(1)


if __name__ == "__main__":
    main()
