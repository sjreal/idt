"""Cheap, deterministic, offline prefilter for the first guardrail tier.

The prefilter applies a small set of substring and regular-expression rules to a
user prompt and adds their capped weights. It performs no network calls, model
invocations, environment lookups, or logging.

Privacy: prompt text is never logged or returned. Only rule names, the numeric
score, and a coarse category are exposed through :class:`PrefilterResult`.

Rules are declared in descending weight order in ``_RULES``; that order is used
both for the stable ``matched_rules`` list and to choose the single reported
category (the highest-weight match wins).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

_OVERRIDE_PHRASES = (
    "ignore previous instructions",
    "ignore all previous",
    "disregard the above",
    "system prompt",
    "you are now",
    "forget your instructions",
    "new instructions",
    "jailbreak",
    "developer mode",
)

_ROLE_MARKERS = (
    "### system",
    "<|system|>",
    "[system]",
    "<<sys>>",
    "</s>",
    "assistant:",
)

_CANARY_PHRASES = (
    "print the canary",
    "reveal the secret",
    "repeat the hidden",
    "output the key",
    "exfiltrate",
    "leak the token",
)

_BASE64_TOKEN_RE = re.compile(r"[A-Za-z0-9+/]{24,}={0,2}")
_HEX_STRING_RE = re.compile(r"\b[0-9a-fA-F]{32,}\b")
_REPETITION_RE = re.compile(r"\b([a-z]{2,})\b(?:\s+\1\b){7,}", re.IGNORECASE)
_LONG_TOKEN_RE = re.compile(r"\S{64,}")


@dataclass(frozen=True)
class PrefilterResult:
    """Outcome of a prefilter scan, without any of the original prompt text."""

    score: float
    matched_rules: list[str] = field(default_factory=list)
    category: str | None = None
    decision: Literal["block", "review", "pass"] = "pass"


def _rot13(text: str) -> str:
    """Return the ROT13 form of a known phrase; user input is never decoded."""
    shifted: list[str] = []
    for char in text:
        if "a" <= char <= "z":
            shifted.append(chr((ord(char) - ord("a") + 13) % 26 + ord("a")))
        elif "A" <= char <= "Z":
            shifted.append(chr((ord(char) - ord("A") + 13) % 26 + ord("A")))
        else:
            shifted.append(char)
    return "".join(shifted)


_ROT13_PHRASES = tuple(
    _rot13(phrase) for phrase in (*_OVERRIDE_PHRASES, *_CANARY_PHRASES)
)


def _has_instruction_override(prompt: str) -> bool:
    """Rule 1: known instruction-override phrases, case-insensitive substrings."""
    lowered = prompt.lower()
    return any(phrase in lowered for phrase in _OVERRIDE_PHRASES)


def _has_role_delimiters(prompt: str) -> bool:
    """Rule 2: chat role or delimiter markers used to forge a system turn."""
    lowered = prompt.lower()
    return any(marker in lowered for marker in _ROLE_MARKERS)


def _has_base64_like_token(prompt: str) -> bool:
    """Rule 3a: 24+ base64 alphabet chars with mixed case and a digit/padding.

    This is a heuristic only: the token is never decoded. Requiring both cases
    and a digit (or ``+``/``/``/``=``) keeps ordinary long lowercase words from
    matching.
    """
    for match in _BASE64_TOKEN_RE.finditer(prompt):
        token = match.group(0)
        has_upper = any(char.isupper() for char in token)
        has_lower = any(char.islower() for char in token)
        has_marker = any(char.isdigit() or char in "+/=" for char in token)
        if has_upper and has_lower and has_marker:
            return True
    return False


def _has_hex_encoded_string(prompt: str) -> bool:
    """Rule 3b: a standalone run of 32+ hex characters (16 or more bytes)."""
    return _HEX_STRING_RE.search(prompt) is not None


def _has_rot13_phrase(prompt: str) -> bool:
    """Rule 3c: ROT13 forms of known suspicious phrases.

    Known phrases are encoded at import time and looked up in the lowercased
    prompt. No decoding or language detection of user input is performed.
    """
    lowered = prompt.lower()
    return any(phrase in lowered for phrase in _ROT13_PHRASES)


def _has_canary_targeting(prompt: str) -> bool:
    """Rule 4: verbs/phrases that try to disclose a canary or hidden secret."""
    lowered = prompt.lower()
    return any(phrase in lowered for phrase in _CANARY_PHRASES)


def _has_repetition_flood(prompt: str) -> bool:
    """Rule 5: the same 2+ letter word repeated at least eight times in a row."""
    return _REPETITION_RE.search(prompt) is not None


def _has_long_single_token(prompt: str) -> bool:
    """Rule 6a: an unusually long whitespace-delimited token (64+ characters)."""
    return _LONG_TOKEN_RE.search(prompt) is not None


def _has_character_mixture_outlier(prompt: str) -> bool:
    """Rule 6b: short prompt with many distinct symbols plus digits and letters.

    A prompt of at least 24 characters is flagged when it has digits, letters,
    four or more distinct punctuation/symbol characters, and symbols make up at
    least one eighth of its length. Ordinary prose never meets this mix.
    """
    if len(prompt) < 24:
        return False
    has_letters = any(char.isalpha() for char in prompt)
    digit_count = sum(char.isdigit() for char in prompt)
    symbols = {char for char in prompt if not char.isalnum() and not char.isspace()}
    if not has_letters or digit_count < 2 or len(symbols) < 4:
        return False
    symbol_count = sum(char in symbols for char in prompt)
    return symbol_count >= max(4, len(prompt) // 8)


Detector = Callable[[str], bool]

# (name, capped weight, category, detector) in descending weight order.
_RULES: tuple[tuple[str, float, str, Detector], ...] = (
    ("instruction_override", 0.50, "instruction_override", _has_instruction_override),
    ("canary_targeting", 0.40, "canary_exfiltration", _has_canary_targeting),
    ("role_delimiters", 0.30, "prompt_injection", _has_role_delimiters),
    ("base64_like_token", 0.20, "encoded_payload", _has_base64_like_token),
    ("hex_encoded_string", 0.20, "encoded_payload", _has_hex_encoded_string),
    ("repetition_flood", 0.20, "prompt_injection", _has_repetition_flood),
    ("long_single_token", 0.20, "obfuscation", _has_long_single_token),
    ("rot13_phrase", 0.10, "obfuscation", _has_rot13_phrase),
    ("character_mixture_outlier", 0.10, "obfuscation", _has_character_mixture_outlier),
)

# Public, transparent view of the scoring model. Each name is added at most once.
RULE_WEIGHTS: dict[str, float] = {name: weight for name, weight, _, _ in _RULES}
RULE_CATEGORIES: dict[str, str] = {name: category for name, _, category, _ in _RULES}


def scan_prompt(
    prompt: str,
    *,
    block_threshold: float = 0.80,
    allow_threshold: float = 0.20,
) -> PrefilterResult:
    """Score a prompt with the additive rule tier.

    A score at or above ``block_threshold`` blocks, a score at or below
    ``allow_threshold`` passes, and anything in between is sent to review.
    The final score is clamped to ``[0, 1]`` and rounded to four decimals so
    repeated calls are byte-for-byte stable.
    """
    matched = [name for name, _weight, _category, detector in _RULES if detector(prompt)]
    raw_score = sum(RULE_WEIGHTS[name] for name in matched)
    score = round(min(1.0, max(0.0, raw_score)), 4)

    if score >= block_threshold:
        decision: Literal["block", "review", "pass"] = "block"
    elif score <= allow_threshold:
        decision = "pass"
    else:
        decision = "review"

    category = next(
        (RULE_CATEGORIES[name] for name in matched),
        None,
    )
    return PrefilterResult(
        score=score,
        matched_rules=matched,
        category=category,
        decision=decision,
    )


def scan_messages(
    messages: list[dict[str, str]],
    *,
    block_threshold: float = 0.80,
    allow_threshold: float = 0.20,
) -> PrefilterResult:
    """Scan the content of the most recent user message.

    Non-string content is treated as empty. When there is no user turn, an empty
    prompt is scanned, giving a zero score and a ``pass`` decision.
    """
    prompt = ""
    for message in reversed(messages):
        if message.get("role") == "user":
            content = message.get("content", "")
            prompt = content if isinstance(content, str) else ""
            break

    return scan_prompt(
        prompt,
        block_threshold=block_threshold,
        allow_threshold=allow_threshold,
    )
