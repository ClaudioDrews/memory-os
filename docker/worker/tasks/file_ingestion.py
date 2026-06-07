"""
Tasks — File-based wiki ingestion (Phase B: continuous).
Receives a file path to a .md file inside the container (e.g. /wiki/concepts/new.md),
extracts frontmatter, generates DENSE + BM25 SPARSE embeddings, upserts into the configured collection.
"""
import logging
import os
import uuid
from pathlib import Path
from datetime import datetime, timezone

import httpx
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import PointStruct

from services.embedding import get_embedding
from services.sparse_embedding import get_sparse_embedding

logger = logging.getLogger("cognitive-worker.file_ingest")

COLLECTION_NAME = os.environ.get("COLLECTION_NAME", "knowledge_base")
QDRANT_HOST = os.environ.get("QDRANT_HOST", "qdrant-maas")
QDRANT_PORT = int(os.environ.get("QDRANT_PORT", "6333"))
WIKI_PATH = os.environ.get("WIKI_PATH", "/wiki")
MAX_TEXT_LEN = 8000


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Extracts YAML frontmatter and returns (metadata, body)."""
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            try:
                import yaml
                meta = yaml.safe_load(parts[1])
                body = parts[2].strip()
                return (meta if isinstance(meta, dict) else {}), body
            except Exception:
                pass
    return {}, text


def get_source_tag(path: Path) -> str:
    """Derives a source tag from the path relative to WIKI_PATH."""
    rel = path.relative_to(WIKI_PATH)
    parts = rel.parts
    if len(parts) > 1:
        return f"wiki-{parts[0]}"
    return "wiki-root"


def get_tags_from_frontmatter(meta: dict) -> list[str]:
    """Extracts tags from frontmatter."""
    tags = meta.get("tags", [])
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",")]
    return tags if isinstance(tags, list) else []


async def upsert_with_dedup(
    qdrant: AsyncQdrantClient,
    collection: str,
    dense_vector: list,
    sparse_vector,
    payload: dict,
    dedup_threshold: float = 0.92,
) -> dict:
    """
    Pre-write dedup: searches for similar neighbors before upserting.
    If cosine similarity >= threshold, merges payload into the existing point.
    Returns a dict with status: 'dedup' or 'upserted'.
    """
    try:
        # Use REST API directly — AsyncQdrantClient doesn't have .search() in qdrant-client 1.18.0
        async with httpx.AsyncClient(timeout=30) as http:
            resp = await http.post(
                f"http://{QDRANT_HOST}:{QDRANT_PORT}/collections/{collection}/points/search",
                json={
                    "vector": {"name": "dense", "vector": dense_vector},
                    "limit": 10,
                    "with_payload": True,
                },
            )
            resp.raise_for_status()
            results = resp.json()["result"]
        for hit in results:
            hit_score = hit["score"]
            hit_id = hit["id"]
            hit_payload = hit.get("payload") or {}
            if hit_score >= dedup_threshold:
                existing_payload = hit_payload
                # Merge: tags (union)
                existing_tags = set(existing_payload.get("tags", []))
                new_tags = set(payload.get("tags", []))
                merged_tags = list(existing_tags | new_tags)
                # Merge: source_type (priority human > procedural > ai)
                st_priority = {"human": 3, "procedural": 2, "ai": 1}
                existing_st = existing_payload.get("source_type", "ai")
                new_st = payload.get("source_type", "ai")
                merged_st = existing_st if st_priority.get(existing_st, 0) >= st_priority.get(new_st, 0) else new_st
                # Merge: last_accessed_at (max)
                existing_la = existing_payload.get("last_accessed_at", payload.get("created_at"))
                new_la = payload.get("last_accessed_at")
                merged_la = max(existing_la, new_la) if existing_la and new_la else (existing_la or new_la)
                # Merge: importance_score (max)
                existing_imp = existing_payload.get("importance_score", 0.5)
                new_imp = payload.get("importance_score", 0.5)
                merged_imp = max(existing_imp, new_imp)
                # Merge: lineage_ids
                existing_lineages = existing_payload.get("lineage_ids", [])
                new_lineages = payload.get("lineage_ids", [])
                if not isinstance(existing_lineages, list):
                    existing_lineages = []
                if not isinstance(new_lineages, list):
                    new_lineages = []
                merged_lineages = list(set(existing_lineages + new_lineages))
                # Apply merge
                await qdrant.set_payload(
                    collection_name=collection,
                    payload={
                        "tags": merged_tags,
                        "source_type": merged_st,
                        "last_accessed_at": merged_la,
                        "importance_score": merged_imp,
                        "lineage_ids": merged_lineages,
                    },
                    points=[hit_id],
                )
                logger.info(f"Dedup: merged into chunk {hit_id} (score={hit_score:.3f})")
                return {
                    "status": "dedup",
                    "existing_id": str(hit_id),
                    "similarity": hit_score,
                }
    except Exception as e:
        logger.warning(f"Error in pre-write dedup: {e}")
    # Fallback: normal upsert
    point = PointStruct(
        id=str(uuid.uuid4()),
        vector={"dense": dense_vector, "sparse": sparse_vector},
        payload=payload,
    )
    await qdrant.upsert(collection_name=collection, points=[point], wait=True)
    return {
        "status": "upserted",
        "id": point.id,
    }


async def ingest_file(
    qdrant: AsyncQdrantClient,
    file_path: str,
) -> dict:
    """
    Ingests a .md file from the vault (dense + BM25 sparse).
    Returns a dict with id and status.
    """
    wiki_root = Path(WIKI_PATH).resolve()
    path = Path(file_path).resolve()
    if not path.is_relative_to(wiki_root) or path.suffix != ".md":
        raise ValueError("file_path must be a .md file under WIKI_PATH")
    if not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        return {"status": "skipped", "reason": "empty file", "file": str(path)}

    meta, body = parse_frontmatter(text)
    source = get_source_tag(path)
    tags = get_tags_from_frontmatter(meta)
    folder_tag = source.replace("wiki-", "")
    if folder_tag not in tags:
        tags.append(folder_tag)

    title = meta.get("title", path.stem)
    embed_text = f"{title}\n\n{body}"[:MAX_TEXT_LEN]

    # Generate embeddings
    try:
        dense_vector = await get_embedding(embed_text)
        sparse_vector = get_sparse_embedding(embed_text)
    except Exception as e:
        logger.error(f"Error generating embedding for {path}: {e}")
        raise

    # Payload
    now = datetime.now(timezone.utc).isoformat()
    
    # Heuristic importance_score based on file path/name
    importance_score = 0.5  # default
    path_str_lower = str(path).lower()
    if any(k in path_str_lower for k in ["architecture", "core", "important"]):
        importance_score = 0.7
    if any(t.lower() in ["important", "critical"] for t in tags):
        importance_score = 0.8
    if any(k in path_str_lower for k in ["draft", "temp", "old"]):
        importance_score = 0.2
    
    payload = {
        "text": embed_text,
        "source": source,
        "tags": tags,
        "created_at": now,
        "reflection_count": 0,
        "last_reflected": None,
        "file_path": str(path),
        "title": title,
        "word_count": len(embed_text.split()),
        # ── Lineage fields (Phase 1) ──
        "lineage_id": None,       # legacy: last lineage that generated this chunk
        "lineage_ids": [],        # Phase 3.5: all accumulated lineages (merge)
        "generation_model": None,
        "generation_context_hash": None,
        "retrieved_chunk_ids": None,
        # ── Decay fields (Phase 2) ──
        "decay_score": 1.0,
        "last_accessed_at": now,
        "importance_score": importance_score,
        "source_type": "human",  # vault files = human origin
        "confidence_score": 1.0,
        "archived": False,
    }

    # Use pre-write dedup instead of direct upsert
    result = await upsert_with_dedup(
        qdrant=qdrant,
        collection=COLLECTION_NAME,
        dense_vector=dense_vector,
        sparse_vector=sparse_vector,
        payload=payload,
        dedup_threshold=0.92,
    )

    if result["status"] == "dedup":
        logger.info(f"File {path.name} deduplicated (merged into {result['existing_id']}) — similarity {result['similarity']:.3f}")
    else:
        logger.info(f"File {path.name} ingested ({source}) — dense+sparse")

    return {
        "id": result.get("id") or result.get("existing_id"),
        "status": result["status"],
        "collection": COLLECTION_NAME,
        "source": source,
        "file": str(path),
        "similarity": result.get("similarity"),
    }
