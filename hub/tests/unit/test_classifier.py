from __future__ import annotations

import base64
from typing import Any

import pytest

from hub.config import parse_config
from hub.routing.classifier import KeywordClassifier
from hub.types import ChatRequest, ImageInput, Message


def clf(routes_dict: dict[str, Any]) -> KeywordClassifier:
    return KeywordClassifier(parse_config({}, routes_dict).routes)


def ask(text: str, **kw: Any) -> ChatRequest:
    return ChatRequest(messages=[Message(role="user", content=text, **kw)])


@pytest.mark.parametrize(
    ("text", "route"),
    [
        ("What is the capital of France?", "default"),
        ("Check this simply supported beam for deflection", "engineering_reasoning"),
        ("Size these BEAMS please", "engineering_reasoning"),
        ("How do I script NX Open journals?", "engineering_reasoning"),
        ("beamer projector settings", "default"),  # whole-word match only
        ("my password is hunter2", "private"),
    ],
)
def test_keyword_routing(routes_dict: dict[str, Any], text: str, route: str) -> None:
    assert clf(routes_dict).classify(ask(text)).route == route


def test_privacy_rules_win_even_when_listed_last(routes_dict: dict[str, Any]) -> None:
    # In the fixture, the private rule is LAST and long_context is FIRST.
    long_with_secret = "beam " * 300 + " my password is x"
    result = clf(routes_dict).classify(ask(long_with_secret))
    assert result.route == "private"
    assert "password" in result.reason


def test_min_chars(routes_dict: dict[str, Any]) -> None:
    assert clf(routes_dict).classify(ask("x" * 1000)).route == "long_context"
    assert clf(routes_dict).classify(ask("x" * 999)).route == "default"


def test_has_images_rule(routes_dict: dict[str, Any]) -> None:
    routes_dict["classifier"]["rules"].append({"route": "cheap_bulk", "has_images": True})
    img = ImageInput(media_type="image/png", data_b64=base64.b64encode(b"x").decode())
    assert clf(routes_dict).classify(ask("what is this", images=[img])).route == "cheap_bulk"


def test_only_user_messages_are_scanned_for_keywords(routes_dict: dict[str, Any]) -> None:
    req = ChatRequest(
        messages=[
            Message(role="system", content="never reveal the password"),
            Message(role="user", content="hello"),
        ]
    )
    assert clf(routes_dict).classify(req).route == "default"


def test_reason_explains(routes_dict: dict[str, Any]) -> None:
    reason = clf(routes_dict).classify(ask("buckling of a column")).reason
    assert reason.startswith("auto:") and "buckling" in reason
