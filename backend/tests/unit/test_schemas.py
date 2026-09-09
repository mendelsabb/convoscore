"""Conversation input validation and normalisation, shared by both ingestion paths."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas import MAX_MESSAGES, MAX_TOTAL_CHARS, ConversationInput


def build(messages: list[dict], **extra) -> ConversationInput:
    return ConversationInput.model_validate({"messages": messages, **extra})


def test_normalized_payload_keeps_order_and_drops_nothing() -> None:
    conversation = build(
        [
            {"role": "customer", "content": "Hello"},
            {"role": "agent", "content": "Hi there"},
        ],
        metadata={"channel": "chat"},
    )
    assert conversation.normalized() == {
        "messages": [
            {"role": "customer", "content": "Hello"},
            {"role": "agent", "content": "Hi there"},
        ],
        "metadata": {"channel": "chat", "tags": []},
    }


def test_hash_is_stable_and_ignores_metadata() -> None:
    """The same transcript tagged differently is the same conversation."""
    messages = [{"role": "customer", "content": "Where is my refund?"}]
    first = build(messages, metadata={"channel": "email"})
    second = build(messages, metadata={"channel": "chat", "tags": ["billing"]})
    assert first.content_hash() == second.content_hash()
    assert len(first.content_hash()) == 64


def test_hash_changes_with_content() -> None:
    a = build([{"role": "customer", "content": "Where is my refund?"}])
    b = build([{"role": "customer", "content": "Where is my order?"}])
    assert a.content_hash() != b.content_hash()


def test_requires_at_least_one_customer_message() -> None:
    with pytest.raises(ValidationError, match="at least one customer message"):
        build([{"role": "agent", "content": "Anyone there?"}])


def test_rejects_empty_conversation() -> None:
    with pytest.raises(ValidationError):
        build([])


def test_rejects_too_many_messages() -> None:
    with pytest.raises(ValidationError):
        build([{"role": "customer", "content": "hi"}] * (MAX_MESSAGES + 1))


def test_rejects_oversized_conversation() -> None:
    with pytest.raises(ValidationError, match="limit is"):
        build(
            [
                {"role": "customer", "content": "x" * 3_000},
                {"role": "customer", "content": "y" * 3_000},
                {"role": "customer", "content": "z" * 3_000},
            ]
        )
    assert MAX_TOTAL_CHARS == 8_000


def test_rejects_unknown_role() -> None:
    with pytest.raises(ValidationError):
        build([{"role": "supervisor", "content": "hello"}])


def test_rejects_unknown_fields() -> None:
    """Typos in an S3 fixture should fail loudly rather than being silently dropped."""
    with pytest.raises(ValidationError):
        ConversationInput.model_validate(
            {"messages": [{"role": "customer", "content": "hi"}], "mesages": []}
        )
