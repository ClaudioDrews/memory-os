"""
Qdrant client — connection and collection validation.
Creates a HYBRID collection (dense + BM25 sparse) if it doesn't exist.
"""
import os
import logging

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, VectorParams, SparseVectorParams, Modifier

logger = logging.getLogger("cognitive-worker.qdrant")

QDRANT_HOST = os.environ.get("QDRANT_HOST", "qdrant-maas")
QDRANT_PORT = int(os.environ.get("QDRANT_PORT", "6333"))
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY", "")
EMBEDDING_DIMS = int(os.environ.get("EMBEDDING_DIMS", "4096"))
COLLECTION_NAME = os.environ.get("COLLECTION_NAME", "knowledge_base")

_client: AsyncQdrantClient | None = None


def get_qdrant_client() -> AsyncQdrantClient:
    """Returns a singleton of the async Qdrant client."""
    global _client
    if _client is None:
        _client = AsyncQdrantClient(host=QDRANT_HOST, port=QDRANT_PORT, api_key=QDRANT_API_KEY, https=False)
        logger.info(f"Connected to Qdrant at {QDRANT_HOST}:{QDRANT_PORT}")
    return _client


async def ensure_collection(client: AsyncQdrantClient) -> None:
    """Ensures the hybrid collection exists with dense + sparse configs.

    Uses ``collection_exists`` rather than listing collections: the logical
    name may be satisfied by a Qdrant **alias** (e.g. ``knowledge_base`` ->
    ``knowledge_base_v2``), which does not appear in ``get_collections``.
    Listing collections and calling ``create_collection`` on an aliased name
    raises a 400 (alias already exists) and crashed the worker on every
    startup.
    """
    try:
        exists = await client.collection_exists(COLLECTION_NAME)
        if exists:
            logger.info(f"Collection {COLLECTION_NAME} already exists")
        else:
            logger.info(
                f"Creating collection {COLLECTION_NAME} with "
                f"dense={EMBEDDING_DIMS} dims + sparse BM25"
            )
            await client.create_collection(
                collection_name=COLLECTION_NAME,
                vectors_config={
                    "dense": VectorParams(
                        size=EMBEDDING_DIMS,
                        distance=Distance.COSINE,
                    )
                },
                sparse_vectors_config={
                    "sparse": SparseVectorParams(modifier=Modifier.IDF)
                },
            )
    except Exception as e:
        logger.error(f"Error validating collection: {e}")
        raise
