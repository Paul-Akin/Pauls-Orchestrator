"""Heuristic classifier for ``route: auto``. No LLM call, deterministic, fast.

Monotonic privacy: rules whose target route is ``local_only`` are always
evaluated *before* any other rule, regardless of their order in YAML. So adding
a "long_context" rule can never cause a message containing "password" to be
sent to a cloud model. The reverse (auto choosing *more* privacy) is allowed.

The :class:`Classifier` protocol is the extension point for an embedding- or
LLM-based classifier later (stage 3+).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from hub.config import ClassifierRule, RoutesConfig
from hub.types import ChatRequest


@dataclass(frozen=True)
class Classification:
    route: str
    reason: str


class Classifier(Protocol):
    def classify(self, req: ChatRequest) -> Classification: ...


@dataclass(frozen=True)
class _CompiledRule:
    rule: ClassifierRule
    keywords: re.Pattern[str] | None


def _compile_keywords(words: list[str]) -> re.Pattern[str] | None:
    if not words:
        return None
    # (?<!\w) / (?!\w) instead of \b so keywords ending in punctuation ("private:")
    # work. An optional plural suffix lets "beam" match "beams".
    alternation = "|".join(re.escape(w.strip()) for w in words if w.strip())
    return re.compile(rf"(?<!\w)(?:{alternation})(?:e?s)?(?!\w)", re.IGNORECASE)


class KeywordClassifier:
    def __init__(self, routes: RoutesConfig) -> None:
        self._default = routes.classifier.default_route
        rules = routes.classifier.rules
        private = [r for r in rules if routes.routes[r.route].local_only]
        others = [r for r in rules if not routes.routes[r.route].local_only]
        self._rules = [
            _CompiledRule(r, _compile_keywords(r.any_keywords)) for r in private + others
        ]

    def classify(self, req: ChatRequest) -> Classification:
        user_text = "\n".join(m.content for m in req.messages if m.role == "user")
        total_chars = sum(len(m.content) for m in req.messages)
        for compiled in self._rules:
            reason = self._match(compiled, user_text, total_chars, req.has_images)
            if reason is not None:
                return Classification(compiled.rule.route, f"auto: {reason}")
        return Classification(self._default, "auto: no rule matched")

    @staticmethod
    def _match(c: _CompiledRule, text: str, chars: int, has_images: bool) -> str | None:
        reasons: list[str] = []
        if c.keywords is not None:
            m = c.keywords.search(text)
            if m is None:
                return None
            reasons.append(f"keyword {m.group(0).lower()!r}")
        if c.rule.min_chars is not None:
            if chars < c.rule.min_chars:
                return None
            reasons.append(f"{chars} chars >= {c.rule.min_chars}")
        if c.rule.has_images is not None:
            if has_images is not c.rule.has_images:
                return None
            reasons.append("has images" if has_images else "no images")
        return ", ".join(reasons)
