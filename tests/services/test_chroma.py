"""
Tests for the Chroma liked-events service.

Embedding generation is mocked so tests run offline without loading
``sentence-transformers`` weights.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from scene_scout.models.enrichment import EnrichedEvent
from scene_scout.models.event import compute_normalized_event_id
from scene_scout.services import chroma as chroma_service
from tests.conftest import TEST_RUN_ID

NORMALIZED_AT = datetime(1993, 7, 4, 18, 0, tzinfo=timezone.utc)
JAZZ_TITLE = "Silver Lake Jazz Night"
ROCK_TITLE = "Downtown Rock Show"
JAZZ_ID = compute_normalized_event_id(JAZZ_TITLE, "Sat, Jul 4 1993", "The Sandlot")
ROCK_ID = compute_normalized_event_id(ROCK_TITLE, "Sat, Jul 4 1993", "The Sandlot")

JAZZ_VECTOR = [1.0, 0.0, 0.0]
ROCK_VECTOR = [0.0, 1.0, 0.0]
NEUTRAL_VECTOR = [0.0, 0.0, 1.0]


def _embedding_for_text(text: str) -> list[float]:
    lowered = text.lower()
    if "jazz" in lowered:
        return JAZZ_VECTOR.copy()
    if "rock" in lowered:
        return ROCK_VECTOR.copy()
    return NEUTRAL_VECTOR.copy()


def _enriched_event(**overrides: object) -> EnrichedEvent:
    payload = {
        "id": JAZZ_ID,
        "title": JAZZ_TITLE,
        "start_datetime": NORMALIZED_AT,
        "venue": "The Sandlot",
        "city": "Los Angeles",
        "url": "https://example.com/jazz-night",
        "is_free": True,
        "description": "An intimate jazz set under the floodlights.",
        "categories": ["Jazz"],
        "vibe_tags": ["intimate"],
        "run_id": TEST_RUN_ID,
        "normalized_at": NORMALIZED_AT,
    }
    payload.update(overrides)
    return EnrichedEvent.model_validate(payload)


@pytest.fixture
def chroma_collection(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Isolated liked-events collection backed by a temp Chroma store."""
    chroma_dir = tmp_path / "vol-chroma"
    chroma_dir.mkdir()
    monkeypatch.setenv("VOL_CHROMA_DIR", str(chroma_dir))
    chroma_service.reset_chroma_client()
    collection = chroma_service.get_liked_events_collection()
    yield collection
    chroma_service.reset_chroma_client()


@pytest.fixture
def mock_embed():
    with patch.object(
        chroma_service,
        "embed",
        side_effect=lambda text: _embedding_for_text(text),
    ) as patched:
        yield patched


def test_embed_returns_float_vector() -> None:
    fake_vector = [0.1, 0.2, 0.3]

    class FakeEncodedVector:
        def tolist(self) -> list[float]:
            return fake_vector

    class FakeModel:
        def encode(self, text: str, *, normalize_embeddings: bool) -> FakeEncodedVector:
            assert text == "jazz night"
            assert normalize_embeddings is True
            return FakeEncodedVector()

    with patch.object(chroma_service, "_get_embedding_model", return_value=FakeModel()):
        assert chroma_service.embed("jazz night") == fake_vector


def test_similarity_score_returns_zero_for_empty_collection(
    chroma_collection,
    mock_embed,
) -> None:
    score = chroma_service.similarity_score(
        _enriched_event(),
        chroma_collection,
    )

    assert score == 0.0
    mock_embed.assert_not_called()


def test_add_liked_event_adds_embedding_to_collection(
    chroma_collection,
    mock_embed,
) -> None:
    event = _enriched_event()

    chroma_service.add_liked_event(event, chroma_collection)

    assert chroma_collection.count() == 1
    stored = chroma_collection.get(ids=[event.id], include=["embeddings", "documents"])
    assert stored["ids"] == [event.id]
    assert chroma_service._vector_to_list(stored["embeddings"][0]) == JAZZ_VECTOR
    assert "jazz" in stored["documents"][0].lower()
    mock_embed.assert_called_once()


def test_similarity_score_returns_cosine_similarity_after_like(
    chroma_collection,
    mock_embed,
) -> None:
    liked = _enriched_event()
    chroma_service.add_liked_event(liked, chroma_collection)

    similar = _enriched_event(
        id=compute_normalized_event_id(
            "Another Jazz Session",
            "Sat, Jul 4 1993",
            "The Sandlot",
        ),
        title="Another Jazz Session",
        description="More jazz under the lights.",
        categories=["Jazz"],
    )
    different = _enriched_event(
        id=ROCK_ID,
        title=ROCK_TITLE,
        description="A loud rock show downtown.",
        categories=["Rock"],
        vibe_tags=["high-energy"],
    )

    similar_score = chroma_service.similarity_score(similar, chroma_collection)
    different_score = chroma_service.similarity_score(different, chroma_collection)

    assert similar_score == pytest.approx(1.0)
    assert different_score == pytest.approx(0.0)


def test_add_liked_event_uses_default_collection_when_none_provided(
    chroma_collection,
    mock_embed,
) -> None:
    chroma_service.add_liked_event(_enriched_event())

    assert chroma_collection.count() == 1
