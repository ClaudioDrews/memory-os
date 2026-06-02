"""
Tasks — Reflection Engine v2.
Reviews old memories, generates insights, CREATES NEW INDEXABLE POINTS in Qdrant.
"""
import logging
import json
import os
import uuid
from datetime import datetime, timezone

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import PointStruct, Filter, FieldCondition, Range

# Alias for qdrant_client.models used in Micro Reflection
import qdrant_client.models as qmodels

from services.llm import ollama_chat
from services.embedding import get_embedding

logger = logging.getLogger("cognitive-worker.reflection")

COLLECTION_NAME = os.environ.get("COLLECTION_NAME", "knowledge_base")
REFLECTION_PROMPT = """
You are a cognitive memory assistant. Analyze the provided memories and extract:
1. Recurring patterns
2. Connections between memories
3. Insights or learnings
4. Suggested actions

Memories:
{memories}

Respond in JSON with keys: patterns, connections, insights, actions.
"""


async def reflect_on_memories(qdrant: AsyncQdrantClient) -> dict:
    """
    Runs a reflection cycle on unreflected or old memories.
    GENERATES NEW indexable points in Qdrant with the insights.
    """
    # Fetch memories with low reflection_count or old
    filter_ref = Filter(
        must=[
            FieldCondition(
                key="reflection_count",
                range=Range(lt=3),
            ),
        ]
    )

    results = await qdrant.scroll(
        collection_name=COLLECTION_NAME,
        scroll_filter=filter_ref,
        limit=5,  # reduced from 20 to avoid LLM timeout
        with_payload=True,
        with_vectors=False,
    )

    points = results[0]  # scroll returns (points, next_page_offset)
    if not points:
        logger.info("No memories need reflection")
        return {"status": "no-op", "processed": 0}

    parent_ids = [p.id for p in points]

    # Prepare batch of memories for LLM
    memories_text = "\n\n".join(
        f"- [{p.id[:8]}] Source: {p.payload.get('source', '?')} | {p.payload.get('text', '')[:400]}"
        for p in points
    )

    prompt = REFLECTION_PROMPT.format(memories=memories_text)

    try:
        response = await ollama_chat(prompt)
        reflection_data = json.loads(response)
    except json.JSONDecodeError:
        logger.warning("Reflection returned invalid JSON, saving raw")
        reflection_data = {"raw": response}
    except Exception as e:
        logger.error(f"Reflection LLM error: {e}")
        raise

    # ─── CREATE NEW INDEXABLE POINT with the insight ─────────────────────────
    # Text for embedding: concatenation of insights
    insight_text = json.dumps(reflection_data, ensure_ascii=False, indent=2)
    reflection_vector = await get_embedding(insight_text)

    now = datetime.now(timezone.utc).isoformat()

    reflection_point = PointStruct(
        id=str(uuid.uuid4()),
        vector={"dense": reflection_vector},
        payload={
            "text": insight_text,
            "source": "reflection",
            "tags": ["reflection", "auto-generated", "insight"],
            "created_at": now,
            "reflection_count": 0,
            "last_reflected": None,
            "parent_ids": parent_ids,
            "title": f"Reflection batch ({len(points)} memories)",
            "word_count": len(insight_text.split()),
        },
    )

    await qdrant.upsert(
        collection_name=COLLECTION_NAME,
        points=[reflection_point],
        wait=True,
    )

    logger.info(f"Reflection point created: {reflection_point.id[:8]} (parents: {len(parent_ids)})")

    # Update metadata of processed memories (lifecycle)
    for point in points:
        new_count = point.payload.get("reflection_count", 0) + 1
        await qdrant.set_payload(
            collection_name=COLLECTION_NAME,
            payload={
                "reflection_count": new_count,
                "last_reflected": now,
            },
            points=[point.id],
        )

    logger.info(f"Reflection completed: {len(points)} memories processed + 1 new indexable point")

    return {
        "status": "reflected",
        "processed": len(points),
        "reflection_point_id": reflection_point.id,
        "reflection": reflection_data,
    }


# ─── MICRO REFLECTION (Phase 3) — Consolidation, not cogitation ────────────

MICRO_REFLECTION_PROMPT = """
You are a cognitive memory assistant. Your job is to CONSOLIDATE existing data, never generate new knowledge.

Analyze the following memory chunk and its similar neighbors. Detect factual contradictions,
inconsistencies, or problematic patterns that could reduce the reliability of this chunk.

Main chunk:
{chunk_text}

Similar neighbors:
{neighbors_text}

Respond in JSON:
{{
    "contradiction_found": true | false,
    "severity": "low" | "medium" | "high",
    "explanation": "Concise description of the problem or confirmation of consistency"
}}
"""

import sqlite3
import os
from typing import Optional

STATE_DB_PATH = os.environ.get("STATE_DB_PATH", "/hermes/state.db")


def get_budget_for_hour(hour_window: str) -> int:
    """Returns how many micro-reflections have run in this hour window."""
    try:
        conn = sqlite3.connect(STATE_DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT count FROM reflection_budget WHERE hour_window = ?",
            (hour_window,),
        )
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else 0
    except Exception as e:
        logger.warning(f"Error checking budget: {e}")
        return 0  # fail-open: if can't check, allow through


def increment_budget(hour_window: str, tokens_used: int = 0):
    """Increments the reflection counter for the current hour."""
    try:
        conn = sqlite3.connect(STATE_DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO reflection_budget (hour_window, count, tokens_used)
            VALUES (?, 1, ?)
            ON CONFLICT(hour_window)
            DO UPDATE SET count = count + 1, tokens_used = tokens_used + ?
        """, (hour_window, tokens_used, tokens_used))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"Error incrementing budget: {e}")


async def micro_reflection(qdrant: AsyncQdrantClient) -> dict:
    """
    Micro-reflection: consolidates freshly ingested chunks using LLM.
    Does NOT create new points. Only updates confidence_score and reflection_notes.
    """
    now = datetime.now(timezone.utc)
    hour_window = now.strftime("%Y-%m-%dT%H")
    
    # ── Budget check ──────────────────────────────────────────────────────
    max_per_hour = int(os.environ.get("MICRO_REFLECTION_MAX_PER_HOUR", "5"))
    current_count = get_budget_for_hour(hour_window)
    
    if current_count >= max_per_hour:
        logger.info(f"Micro-reflection budget exhausted for {hour_window} ({current_count}/{max_per_hour})")
        return {"status": "budget_exceeded", "processed": 0}
    
    # ── Select chunks ─────────────────────────────────────────────────────
    max_chunks = int(os.environ.get("MICRO_REFLECTION_MAX_CHUNKS", "10"))
    
    filter_chunks = Filter(
        must=[
            FieldCondition(
                key="reflection_count",
                range=Range(lt=3),
            ),
            FieldCondition(
                key="archived",
                match=qmodels.MatchValue(value=False),
            ),
        ]
    )
    
    try:
        # Order by created_at DESC via scroll (Qdrant has no direct sort; use scroll with limit)
        results = await qdrant.scroll(
            collection_name=COLLECTION_NAME,
            scroll_filter=filter_chunks,
            limit=max_chunks,
            with_payload=True,
            with_vectors=False,
        )
        points = results[0]
    except Exception as e:
        logger.warning(f"Error fetching chunks for reflection: {e}")
        return {"status": "error", "error": str(e)}
    
    if not points:
        logger.info("No eligible chunks for micro-reflection")
        return {"status": "no-op", "processed": 0}
    
    processed = 0
    contradictions = 0
    consistencies = 0
    
    for point in points:
        chunk_text = point.payload.get("text", "")
        chunk_id = point.id
        
        if not chunk_text:
            continue
        
        # Fetch similar neighbors via REST API
        try:
            import httpx
            point_data = await qdrant.retrieve(
                collection_name=COLLECTION_NAME,
                ids=[chunk_id],
                with_vectors=True,
            )
            if not point_data or not point_data[0].vector:
                logger.warning(f"Could not get vector for chunk {chunk_id}")
                continue
            
            vector = point_data[0].vector.get("dense") if isinstance(point_data[0].vector, dict) else point_data[0].vector
            
            async with httpx.AsyncClient() as client:
                qdrant_host = os.environ.get("QDRANT_HOST", "qdrant-maas")
                qdrant_port = int(os.environ.get("QDRANT_PORT", "6333"))
                resp = await client.post(
                    f"http://{qdrant_host}:{qdrant_port}/collections/{COLLECTION_NAME}/points/search",
                    json={
                        "vector": {"name": "dense", "vector": vector},
                        "limit": 4,
                        "with_payload": True,
                    },
                    timeout=10,
                )
                resp.raise_for_status()
                neighbors = resp.json().get("result", [])
            
            # Filter out the chunk itself
            neighbor_texts = []
            for n in neighbors:
                if n["id"] != chunk_id and n.get("payload", {}).get("text"):
                    neighbor_texts.append(f"[{str(n['id'])[:8]}] {n['payload']['text'][:300]}")
            
            if len(neighbor_texts) < 2:
                logger.info(f"Chunk {chunk_id[:8]} has too few neighbors, skipping")
                continue
            
        except Exception as e:
            logger.warning(f"Error fetching neighbors for {chunk_id}: {e}")
            continue
        
        # LLM analysis
        neighbors_text = "\n\n".join(neighbor_texts[:3])
        prompt = MICRO_REFLECTION_PROMPT.format(
            chunk_text=chunk_text[:600],
            neighbors_text=neighbors_text,
        )
        
        try:
            response = await ollama_chat(prompt)
            # Extract JSON from response
            import re
            json_match = re.search(r'\{[^}]*\}', response)
            if json_match:
                analysis = json.loads(json_match.group())
            else:
                # Try to parse the whole response
                analysis = json.loads(response.strip())
        except json.JSONDecodeError:
            logger.warning(f"LLM response is not valid JSON for chunk {chunk_id[:8]}: {response[:100]}")
            analysis = {"contradiction_found": False, "severity": "low", "explanation": "Could not analyze"}
        except Exception as e:
            logger.warning(f"LLM error for chunk {chunk_id[:8]}: {e}")
            continue
        
        # Apply result
        current_confidence = point.payload.get("confidence_score", 1.0)
        contradiction_found = analysis.get("contradiction_found", False)
        severity = analysis.get("severity", "low")
        explanation = analysis.get("explanation", "")
        
        if contradiction_found:
            severity_mult = {"low": 0.05, "medium": 0.1, "high": 0.2}.get(severity, 0.1)
            new_confidence = max(0.0, current_confidence - severity_mult)
            contradictions += 1
            reflection_note = f"[CONTRADICTION {severity.upper()}] {explanation}"
        else:
            new_confidence = min(1.0, current_confidence + 0.05)
            consistencies += 1
            reflection_note = f"[CONSISTENT] {explanation}"
        
        # Update payload in Qdrant
        new_count = point.payload.get("reflection_count", 0) + 1
        try:
            await qdrant.set_payload(
                collection_name=COLLECTION_NAME,
                payload={
                    "confidence_score": round(new_confidence, 3),
                    "reflection_count": new_count,
                    "last_reflected": now.isoformat(),
                    "reflection_notes": reflection_note,
                },
                points=[chunk_id],
            )
            processed += 1
            logger.info(f"Micro-reflection {chunk_id[:8]}: {reflection_note[:80]}")
        except Exception as e:
            logger.warning(f"Error updating chunk {chunk_id}: {e}")
    
    # Increment budget
    increment_budget(hour_window, tokens_used=0)
    
    logger.info(f"Micro-reflection completed: {processed} chunks, {contradictions} contradictions, {consistencies} consistent")
    
    return {
        "status": "completed",
        "processed": processed,
        "contradictions": contradictions,
        "consistencies": consistencies,
        "budget_hour": hour_window,
        "budget_used": current_count + 1,
    }
