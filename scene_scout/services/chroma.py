"""
Chroma vector store for SceneScout liked-event embeddings.

Uses ``sentence-transformers`` for local embedding generation and Chroma for
persistent storage under ``vol-chroma/``. Ranking consumes
:func:`similarity_score` as the ``semantic_similarity`` cold-start signal.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

import chromadb

from scene_scout.chroma_config import EMBEDDING_MODEL_NAME, LIKED_EVENTS_COLLECTION_NAME
from scene_scout.config import vol_chroma_dir
from scene_scout.models.enrichment import EnrichedEvent

if TYPE_CHECKING:
    from chromadb.api.models.Collection import Collection

_model: Any | None = None
_client: chromadb.ClientAPI | None = None


def _get_embedding_model() -> Any:
    """Return a lazily loaded ``SentenceTransformer`` model."""
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer

        _model = SentenceTransformer(EMBEDDING_MODEL_NAME)
    return _model


def reset_chroma_client() -> None:
    """Clear the cached Chroma client (for tests)."""
    global _client
    _client = None


def get_chroma_client(*, persist_path: str | None = None) -> chromadb.ClientAPI:
    """Return a persistent Chroma client rooted at ``vol-chroma``."""
    global _client
    if _client is None:
        _client = chromadb.PersistentClient(path=persist_path or str(vol_chroma_dir()))
    return _client


def get_liked_events_collection(
    *,
    client: chromadb.ClientAPI | None = None,
) -> Collection:
    """Return the liked-events collection, creating it when missing."""
    chroma_client = client or get_chroma_client()
    return chroma_client.get_or_create_collection(
        name=LIKED_EVENTS_COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )


def event_embedding_text(event: EnrichedEvent) -> str:
    """Build the text blob embedded for an event."""
    parts = [
        event.title,
        event.description,
        " ".join(event.categories),
        " ".join(event.vibe_tags),
        " ".join(performer.name for performer in event.performers),
    ]
    return " ".join(part.strip() for part in parts if part and part.strip())


def embed(text: str) -> list[float]:
    """Return a normalized embedding vector for ``text``.

    Parameters
    ----------
    text : str
        Input text to embed.

    Returns
    -------
    list[float]
        Embedding vector from ``sentence-transformers``.
    """
    vector = _get_embedding_model().encode(text, normalize_embeddings=True)
    return vector.tolist()


def _vector_to_list(vector: Any) -> list[float]:
    """Normalize an embedding vector from Chroma or NumPy into a Python list."""
    if hasattr(vector, "tolist"):
        return [float(value) for value in vector.tolist()]
    return [float(value) for value in vector]


def cosine_similarity(left: list[float], right: list[float]) -> float:
    """Return cosine similarity between two vectors."""
    if len(left) != len(right) or not left:
        return 0.0

    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (left_norm * right_norm)


def similarity_score(event: EnrichedEvent, collection: Collection) -> float:
    """Return max cosine similarity between ``event`` and liked events.

    Parameters
    ----------
    event : EnrichedEvent
        Candidate event to score.
    collection : Collection
        Chroma collection of previously liked events.

    Returns
    -------
    float
        Highest cosine similarity in ``[0.0, 1.0]``, or ``0.0`` when the
        collection is empty (cold start).
    """
    if collection.count() == 0:
        return 0.0

    query_vector = embed(event_embedding_text(event))
    results = collection.query(
        query_embeddings=[query_vector],
        n_results=collection.count(),
        include=["embeddings"],
    )

    stored_embeddings = results.get("embeddings")
    if stored_embeddings is None or len(stored_embeddings) == 0:
        return 0.0

    first_result = stored_embeddings[0]
    if first_result is None or len(first_result) == 0:
        return 0.0

    return max(
        cosine_similarity(query_vector, _vector_to_list(stored_vector))
        for stored_vector in first_result
    )


def add_liked_event(
    event: EnrichedEvent,
    collection: Collection | None = None,
) -> None:
    """Add an liked event embedding to the Chroma collection.

    Parameters
    ----------
    event : EnrichedEvent
        Event the user clicked or otherwise liked.
    collection : Collection, optional
        Target collection. Defaults to :func:`get_liked_events_collection`.
    """
    target = collection or get_liked_events_collection()
    text = event_embedding_text(event)
    vector = embed(text)
    target.upsert(
        ids=[event.id],
        embeddings=[vector],
        documents=[text],
        metadatas=[{"title": event.title, "city": event.city}],
    )
