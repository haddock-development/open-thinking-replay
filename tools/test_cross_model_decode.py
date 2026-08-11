#!/usr/bin/env python3
"""Unit tests for tools/cross_model_decode.py's pure post-processing logic.

Runnable with plain python (no pytest required), no network calls:

    cd open-thinking-replay
    python tools/test_cross_model_decode.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Configure bedrock mode deterministically before importing server (cross_model_decode.py
# imports it transitively via tools.extract_cot) — same convention as test_thinking_adapter.py.
os.environ.update(
    {
        "UPSTREAM_MODE": "bedrock",
        "UPSTREAM_BASE": "http://api.model-eval.woa.com",
        "UPSTREAM_PROVIDER": "aws_third",
        "DISTILL_APP_ID": "TEST_APP",
        "DISTILL_APP_KEY": "TEST_KEY",
        "MODEL": "anthropic.claude-sonnet-4-5-20250929-v1:0",
    }
)
for _k in ("UPSTREAM_KEY", "ANTHROPIC_API_KEY", "DISTILL_MODEL_MARKER"):
    os.environ.pop(_k, None)

from tools import cross_model_decode as cmd  # noqa: E402

_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" :: {detail}" if detail and not cond else ""))
    if not cond:
        _failures.append(name)


def test_clean_transcription_with_closing_tag() -> None:
    """The common case: the decoder transcribes cleanly and closes the tag, with no
    trailing commentary — everything up to and including the close tag is kept verbatim."""
    completion = "hello world\n</thinking-copy>"
    check(
        "clean_close_tag",
        cmd.extract_transcription(completion) == "hello world",
        cmd.extract_transcription(completion),
    )


def test_rambling_after_close_tag_is_discarded() -> None:
    """docs/reproduction-log.md §8.6's observed shape: the decoder correctly transcribes the
    sealed reasoning, closes the tag, then keeps talking — the trailing commentary must be
    discarded, not concatenated onto the recovered text."""
    completion = (
        "Units digit of 7 cycles: 7,9,3,1 with period 4.\n123 mod 4 = 3, so third in cycle "
        "= 3.\n</thinking-copy>\n\nI notice you've asked me to continue, but there's no "
        "prior context in this conversation to continue from."
    )
    result = cmd.extract_transcription(completion)
    check("rambling_discarded", "I notice you've asked" not in result, result)
    check(
        "rambling_transcription_exact",
        result == "Units digit of 7 cycles: 7,9,3,1 with period 4.\n123 mod 4 = 3, so third in cycle = 3.",
        result,
    )


def test_missing_closing_tag_returns_everything() -> None:
    """If max_tokens cuts the completion off before the model emits a closing tag, there is
    no unambiguous boundary — return everything rather than silently truncating further, so
    the caller's length-oracle check has the best chance of still matching (or clearly
    failing, if the completion really was cut short)."""
    completion = "partial reasoning that never closes"
    check(
        "no_close_tag_passthrough",
        cmd.extract_transcription(completion) == "partial reasoning that never closes",
    )


def test_empty_completion() -> None:
    check("empty_completion", cmd.extract_transcription("") == "")


def test_immediate_close_tag() -> None:
    """The decoder claims there is nothing to transcribe and closes immediately — this is
    the systematic Opus-5-signature failure mode documented in §8.6: an empty transcription,
    which will then correctly fail the caller's length-oracle check against any non-zero
    sealed length."""
    completion = "</thinking-copy>\n\nI don't see any previous reasoning attached."
    check("immediate_close_is_empty", cmd.extract_transcription(completion) == "")


def test_refusal_detected_before_length_check() -> None:
    """The measured false positive this filter exists for: a decoder refusal that happens to
    be exactly as long as the sealed plaintext (506 B in the observed case) would otherwise
    pass a length-only check. See docs/reproduction-log.md §8.6."""
    refusal = (
        "I don't have any previous context or attached reasoning from a prior turn in this "
        "conversation. This appears to be the first message in our exchange, so there's nothing "
        "for me to transcribe."
    )
    check("refusal_detected", cmd.looks_like_decoder_refusal(refusal) is True)


def test_genuine_reasoning_containing_refusal_words_is_kept() -> None:
    """Ordinary reasoning frequently contains "I can't ..." mid-stream. Only whole-message
    refusals should be filtered, or the filter throws away real recoveries — the same failure
    extract_pipeline_capture.py was fixed for (docs/reproduction-log.md §8.3)."""
    genuine = (
        "Let me check the divisor condition. I can't just assume n+1 divides 6 without "
        "verifying, so let me expand (n+3)(n+4) mod (n+1) = 6."
    )
    check("genuine_reasoning_kept", cmd.looks_like_decoder_refusal(genuine) is False)


def test_empty_text_is_treated_as_refusal_free() -> None:
    """An empty string carries no refusal marker; the caller's length check rejects it anyway."""
    check("empty_not_refusal", cmd.looks_like_decoder_refusal("") is False)


def main() -> int:
    test_clean_transcription_with_closing_tag()
    test_rambling_after_close_tag_is_discarded()
    test_missing_closing_tag_returns_everything()
    test_empty_completion()
    test_immediate_close_tag()
    test_refusal_detected_before_length_check()
    test_genuine_reasoning_containing_refusal_words_is_kept()
    test_empty_text_is_treated_as_refusal_free()

    total = 8
    passed = total - len(_failures)
    print(f"\n{passed}/{total} passed")
    if _failures:
        print("FAILED:", ", ".join(_failures))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
