#!/usr/bin/env python3
"""Unit tests for the pipeline-capture extractor's pure logic (chaining, block
selection, prefix reconstruction) — no network calls.

Runnable with plain python (no pytest required):

    cd open-thinking-replay
    python tools/test_extract_pipeline_capture.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# extract_pipeline_capture imports server, which resolves its config at import time —
# pin a harmless bedrock-mode config so it doesn't accidentally pick up a real .env.
os.environ.update(
    {
        "UPSTREAM_MODE": "bedrock",
        "UPSTREAM_PROVIDER": "aws_third",
        "DISTILL_APP_ID": "TEST_APP",
        "DISTILL_APP_KEY": "TEST_KEY",
        "MODEL": "anthropic.claude-opus-5",
    }
)
for _k in ("UPSTREAM_KEY", "ANTHROPIC_API_KEY", "DISTILL_MODEL_MARKER"):
    os.environ.pop(_k, None)

from tools import extract_pipeline_capture as epc  # noqa: E402
from tools.extract_cot import VERBATIM_ELICITOR  # noqa: E402
from unittest.mock import patch  # noqa: E402
import base64  # noqa: E402
import struct  # noqa: E402

_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" :: {detail}" if detail and not cond else ""))
    if not cond:
        _failures.append(name)


def _record(model: str, messages: list, content: list, route: str = "bedrock") -> dict:
    return {
        "route": route,
        "model": model,
        "request": {"messages": messages},
        "response": {"content": content, "stop_reason": "end_turn"},
        "status": 200,
    }


def test_chain_two_hop_exact() -> None:
    """B's messages == A.messages + [assistant_turn_from_A] exactly (no extra turns)."""
    r1 = _record("m", [{"role": "user", "content": "hi"}], [{"type": "text", "text": "ok"}])
    r2 = _record(
        "m",
        [{"role": "user", "content": "hi"}, {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}, {"role": "user", "content": "more"}],
        [{"type": "text", "text": "done"}],
    )
    chains = epc.chain_records([r1, r2])
    check("two_hop_exact_one_chain", len(chains) == 1, str(len(chains)))
    check("two_hop_exact_order", chains and chains[0] == [r1, r2])


def test_chain_with_tool_result_gap() -> None:
    """B appends assistant_turn_from_A PLUS a trailing tool_result turn — the common
    real-world shape this extractor must chain correctly (see docs/reproduction-log.md §8.2)."""
    a_content = [{"type": "thinking", "thinking": "", "signature": "sigA"}, {"type": "tool_use", "id": "t1", "name": "f", "input": {}}]
    r1 = _record("m", [{"role": "user", "content": "hi"}], a_content)
    r2 = _record(
        "m",
        [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": a_content},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "42"}]},
        ],
        [{"type": "text", "text": "done"}],
    )
    chains = epc.chain_records([r1, r2])
    check("tool_result_gap_one_chain", len(chains) == 1, str(len(chains)))
    check("tool_result_gap_order", chains and chains[0] == [r1, r2])


def test_chain_unrelated_records_stay_separate() -> None:
    r1 = _record("m", [{"role": "user", "content": "topic A"}], [{"type": "text", "text": "a"}])
    r2 = _record("m", [{"role": "user", "content": "totally unrelated topic B"}], [{"type": "text", "text": "b"}])
    chains = epc.chain_records([r1, r2])
    check("unrelated_two_chains", len(chains) == 2, str(len(chains)))


def test_chain_three_hop() -> None:
    a_content = [{"type": "text", "text": "step1"}]
    r1 = _record("m", [{"role": "user", "content": "u1"}], a_content)
    b_msgs = [{"role": "user", "content": "u1"}, {"role": "assistant", "content": a_content}, {"role": "user", "content": "u2"}]
    b_content = [{"type": "text", "text": "step2"}]
    r2 = _record("m", b_msgs, b_content)
    c_msgs = b_msgs + [{"role": "assistant", "content": b_content}, {"role": "user", "content": "u3"}]
    r3 = _record("m", c_msgs, [{"type": "text", "text": "step3"}])
    chains = epc.chain_records([r1, r2, r3])
    check("three_hop_one_chain", len(chains) == 1, str(len(chains)))
    check("three_hop_order", chains and chains[0] == [r1, r2, r3])


def test_thinking_block_indices_only_signed() -> None:
    content = [
        {"type": "thinking", "thinking": "", "signature": "sig1"},
        {"type": "text", "text": "x"},
        {"type": "thinking", "thinking": "visible summary", "signature": ""},  # no signature -> not extractable
    ]
    r = _record("m", [{"role": "user", "content": "u"}], content)
    check("only_signed_blocks_found", epc.thinking_block_indices(r) == [0], str(epc.thinking_block_indices(r)))


def test_assistant_content_upto_blanks_thinking_and_truncates() -> None:
    content = [
        {"type": "thinking", "thinking": "real cot", "signature": "sig1"},
        {"type": "tool_use", "id": "t1", "name": "f", "input": {}},
        {"type": "thinking", "thinking": "a later, unrelated block", "signature": "sig2"},
    ]
    out = epc.assistant_content_upto(content, 0)
    check("upto_truncates_after_target", len(out) == 1, str(out))
    check("upto_blanks_thinking_text", out[0]["thinking"] == "", str(out))
    check("upto_keeps_signature", out[0]["signature"] == "sig1")

    out2 = epc.assistant_content_upto(content, 1)
    check("upto_includes_following_tool_use", len(out2) == 2 and out2[1]["type"] == "tool_use", str(out2))


def test_sanitize_standalone_user_message_strips_tool_result_wrapper() -> None:
    msg = {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "file contents here"}],
    }
    sanitized = epc.sanitize_standalone_user_message(msg)
    check("sanitize_drops_tool_result_type", sanitized["content"] != msg["content"])
    check("sanitize_keeps_inner_text", "file contents here" in sanitized["content"], sanitized["content"])
    check("sanitize_is_plain_user_text", isinstance(sanitized["content"], str))


def test_sanitize_standalone_user_message_passthrough_for_plain_text() -> None:
    msg = {"role": "user", "content": "a plain instruction"}
    check("sanitize_passthrough", epc.sanitize_standalone_user_message(msg) == msg)


def test_sanitize_standalone_user_message_handles_list_of_tool_results() -> None:
    msg = {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "first"}]},
            {"type": "tool_result", "tool_use_id": "t2", "content": "second"},
        ],
    }
    sanitized = epc.sanitize_standalone_user_message(msg)
    check("sanitize_joins_multiple_tool_results", "first" in sanitized["content"] and "second" in sanitized["content"], sanitized["content"])


def test_last_user_message() -> None:
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": [{"type": "text", "text": "reply"}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "42"}]},
    ]
    lu = epc.last_user_message(messages)
    check("last_user_is_the_tool_result_turn", lu is not None and lu["role"] == "user" and isinstance(lu["content"], list))


def test_looks_refused() -> None:
    check("refusal_detected", epc.looks_refused("I can't produce that as a verbatim transcript."))
    check("non_refusal_not_flagged", not epc.looks_refused("We need Pythagorean triples with perimeter 84."))


# --------------------------------------------------------------------------------- #
# Tiered verification for large blocks (mocked upstream — no network).
# --------------------------------------------------------------------------------- #
def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | 0x80 if n else b)
        if not n:
            return bytes(out)


def _synthetic_signature(sealed_bytes: int) -> str:
    """Same construction as test_thinking_adapter.py's envelope() helper."""
    def ld(field_no: int, payload: bytes) -> bytes:
        return _varint(field_no << 3 | 2) + _varint(len(payload)) + payload

    ciphertext = b"\x11" * (sealed_bytes + 16)  # + GCM tag
    header = ld(6, b"claude-test") + ld(8, b"thinking")
    inner = ld(1, header) + ld(2, b"\x00" * 12) + ld(5, ciphertext)
    return base64.b64encode(ld(2, inner)).decode()


def _large_record(sealed_bytes: int) -> dict:
    sig = _synthetic_signature(sealed_bytes)
    return {
        "route": "bedrock",
        "model": "anthropic.claude-opus-5",
        "request": {"messages": [{"role": "user", "content": "solve a hard problem"}]},
        "response": {"content": [{"type": "thinking", "thinking": "", "signature": sig}]},
    }


def _mock_call_upstream(responses):
    """Returns a stand-in for call_upstream(route, model, body, label) cycling through
    `responses` (each a plain string of reply text); records call count on the list."""
    calls = {"n": 0}

    def _mock(route, model, body, label, **kwargs):
        i = min(calls["n"], len(responses) - 1)
        calls["n"] += 1
        text = responses[i]
        return {"data": {"content": [{"type": "text", "text": text}]}, "cost": {"usd": 0.01}}

    return _mock, calls


def test_large_block_defaults_to_length_only_with_reduced_attempts() -> None:
    target = 5000
    text = "x" * target
    mock_fn, calls = _mock_call_upstream([text])
    with patch.object(epc, "call_upstream", mock_fn):
        ext = epc.extract_block(
            chain_id=0, turn_index=0, record=_large_record(target), block_index=0,
            max_tokens=8000, attempts=4, verify="strict", accept_within=0,
            large_threshold=4000, large_max_attempts=2, near_miss_bytes=100,
            do_strict_sample=False,
        )
    check("large_not_sampled_is_large", ext.is_large)
    check("large_not_sampled_verification_length", ext.verification == "length", ext.verification)
    check("large_not_sampled_one_call_only", calls["n"] == 1, f"calls={calls['n']}")
    check("large_not_sampled_no_near_miss", not ext.near_miss)


def test_large_block_sampled_confirms_strict_on_identical_repeat() -> None:
    target = 5000
    text = "y" * target
    mock_fn, calls = _mock_call_upstream([text, text])  # confirmation replay repeats verbatim
    with patch.object(epc, "call_upstream", mock_fn):
        ext = epc.extract_block(
            chain_id=0, turn_index=0, record=_large_record(target), block_index=0,
            max_tokens=8000, attempts=4, verify="strict", accept_within=0,
            large_threshold=4000, large_max_attempts=2, near_miss_bytes=100,
            do_strict_sample=True,
        )
    check("large_sampled_upgraded_to_strict", ext.verification == "strict", ext.verification)
    check("large_sampled_two_calls", calls["n"] == 2, f"calls={calls['n']}")
    check("large_sampled_no_near_miss_when_identical", not ext.near_miss)


def test_large_block_near_miss_flagged_but_accepted() -> None:
    target = 5000
    first = "z" * target
    second = "z" * (target - 40)  # different length -> different SHA256, 40 B apart
    mock_fn, calls = _mock_call_upstream([first, second])
    with patch.object(epc, "call_upstream", mock_fn):
        ext = epc.extract_block(
            chain_id=0, turn_index=0, record=_large_record(target), block_index=0,
            max_tokens=8000, attempts=4, verify="strict", accept_within=0,
            large_threshold=4000, large_max_attempts=2, near_miss_bytes=100,
            do_strict_sample=True,
        )
    check("near_miss_flagged", ext.near_miss)
    check("near_miss_diff_is_40", ext.near_miss_diff == 40, str(ext.near_miss_diff))
    check("near_miss_still_accepted_at_length", ext.verification == "length", ext.verification)
    # Cost fix (2026-08-11): a sampled large block now uses the SAME cheap length-tier
    # accept as any other large block (verified=True on the first exact-length hit) —
    # sampling only adds an informational confirmation replay (the near_miss flag above),
    # it no longer gates acceptance. Previously this asserted `verified is False` because
    # sampled blocks were forced through the full strict-tier budget in the main loop.
    check("near_miss_verified_true_at_length_tier", ext.verified is True, ext.verified)


def test_large_block_sampled_never_exceeds_tiered_call_budget() -> None:
    """Regression test for the cost bug found on the 2026-08-11 full-batch run: a sampled
    large block used to fall back to the full `attempts` (strict-tier) budget in the main
    loop instead of the cheap `large_max_attempts` tiered budget, averaging 4.4 calls/block
    at ~50% higher cost than non-sampled large blocks on real data. A sampled block must
    never cost more than large_max_attempts (main loop) + 1 (confirmation replay)."""
    target = 5000
    # No attempt ever hits the exact target length -> main loop exhausts its FULL budget.
    # With the bug, that budget was `attempts` (4); fixed, it must be `large_max_attempts`.
    responses = ["v" * (target - 999) for _ in range(6)]
    mock_fn, calls = _mock_call_upstream(responses)
    with patch.object(epc, "call_upstream", mock_fn):
        epc.extract_block(
            chain_id=0, turn_index=0, record=_large_record(target), block_index=0,
            max_tokens=8000, attempts=4, verify="strict", accept_within=0,
            large_threshold=4000, large_max_attempts=2, near_miss_bytes=100,
            do_strict_sample=True,
        )
    check(
        "sampled_call_budget_capped_at_tiered_plus_confirmation",
        calls["n"] <= 2,  # large_max_attempts(2) main-loop calls, no confirmation fires
        f"calls={calls['n']} (expected <= 2, was capped at attempts=4 before the fix)",
    )


def test_large_block_far_miss_not_flagged_near_miss() -> None:
    target = 5000
    first = "w" * target
    second = "w" * (target - 500)  # 500 B apart — beyond the 100 B near-miss window
    mock_fn, calls = _mock_call_upstream([first, second])
    with patch.object(epc, "call_upstream", mock_fn):
        ext = epc.extract_block(
            chain_id=0, turn_index=0, record=_large_record(target), block_index=0,
            max_tokens=8000, attempts=4, verify="strict", accept_within=0,
            large_threshold=4000, large_max_attempts=2, near_miss_bytes=100,
            do_strict_sample=True,
        )
    check("far_miss_not_flagged", not ext.near_miss)
    check("far_miss_still_length", ext.verification == "length", ext.verification)


def test_exact_length_match_not_discarded_as_refusal() -> None:
    """Regression test for the 2026-08-10 full-pipeline-run bug: a byte-exact recovery
    (satisfies the length oracle) must never be discarded just because it contains a
    common English phrase that superficially matches a refusal marker (e.g. "I can't
    just do X" appearing naturally inside genuine reasoning about a sub-task)."""
    target = 5000
    # Exactly `target` bytes, but contains a phrase in _REFUSAL_MARKERS ("i can't").
    padding = "x" * (target - len("i can't just do this the naive way, so "))
    text = "i can't just do this the naive way, so " + padding
    assert len(text.encode("utf-8")) == target
    mock_fn, calls = _mock_call_upstream([text, text])
    with patch.object(epc, "call_upstream", mock_fn):
        ext = epc.extract_block(
            chain_id=0, turn_index=0, record=_large_record(target), block_index=0,
            max_tokens=8000, attempts=4, verify="strict", accept_within=0,
            large_threshold=4000, large_max_attempts=2, near_miss_bytes=100,
            do_strict_sample=True,
        )
    check("exact_match_verified_despite_refusal_phrase", ext.verified, str(ext.verified))
    check("exact_match_upgraded_to_strict", ext.verification == "strict", ext.verification)
    check("exact_match_recovered_full_text", ext.cot == text)
    check("exact_match_no_refusal_note", not any("refusal" in n for n in ext.notes), ext.notes)


def test_generalized_near_miss_on_non_sampled_block() -> None:
    """A large block that is NOT sampled for strict confirmation, but whose closest attempt
    is within near_miss_bytes of the sealed length, should be flagged near_miss and use the
    CLOSEST (not longest) attempt — regression test for the near-miss generalization."""
    target = 5000
    far = "a" * (target + 900)  # longest attempt, but far from target
    close = "b" * (target - 30)  # not the longest, but within the 100 B near-miss window
    mock_fn, calls = _mock_call_upstream([far, close])
    with patch.object(epc, "call_upstream", mock_fn):
        ext = epc.extract_block(
            chain_id=0, turn_index=0, record=_large_record(target), block_index=0,
            max_tokens=8000, attempts=2, verify="strict", accept_within=0,
            large_threshold=4000, large_max_attempts=2, near_miss_bytes=100,
            do_strict_sample=False,
        )
    check("generalized_near_miss_flagged", ext.near_miss)
    check("generalized_near_miss_diff_is_30", ext.near_miss_diff == 30, str(ext.near_miss_diff))
    check("generalized_near_miss_uses_closest_not_longest", ext.cot == close, ext.cot[:20])
    check("generalized_near_miss_verification_approximate", ext.verification == "approximate", ext.verification)
    check("generalized_near_miss_not_verified_under_strict", ext.verified is False, ext.verified)


def test_free_summary_text_reads_the_blocks_own_thinking_field() -> None:
    record = _record(
        "anthropic.claude-opus-5",
        [{"role": "user", "content": "hi"}],
        [{"type": "thinking", "thinking": "I'm deciding X because Y.", "signature": "sig123"}],
    )
    check("free_summary_text_found", epc.free_summary_text(record, 0) == "I'm deciding X because Y.")
    check("free_summary_text_empty_for_bad_index", epc.free_summary_text(record, 5) == "")


def test_free_summary_extraction_costs_nothing_and_is_unverified() -> None:
    record = _record(
        "anthropic.claude-opus-5",
        [{"role": "user", "content": "hi"}],
        [{"type": "thinking", "thinking": "I'm deciding X because Y.", "signature": "sig123"}],
    )
    ext = epc.free_summary_extraction(chain_id=1, turn_index=2, block_index=0, record=record, target=500)
    check("free_summary_zero_cost", ext.cost_usd == 0.0)
    check("free_summary_zero_attempts", ext.attempts == 0)
    check("free_summary_not_verified", ext.verified is False)
    check("free_summary_tier_label", ext.verification == "summary")
    check("free_summary_cot_matches", ext.cot == "I'm deciding X because Y.")
    check("free_summary_sealed_bytes_passthrough", ext.sealed_bytes == 500)


def test_small_block_ignores_tiering() -> None:
    target = 500  # below the 4000 B large threshold
    text = "s" * target
    mock_fn, calls = _mock_call_upstream([text, text])
    with patch.object(epc, "call_upstream", mock_fn):
        ext = epc.extract_block(
            chain_id=0, turn_index=0, record=_large_record(target), block_index=0,
            max_tokens=8000, attempts=4, verify="strict", accept_within=0,
            large_threshold=4000, large_max_attempts=2, near_miss_bytes=100,
            do_strict_sample=False,
        )
    check("small_block_not_large", not ext.is_large)
    check("small_block_reaches_strict", ext.verification == "strict", ext.verification)


def main() -> int:
    for fn in [
        test_chain_two_hop_exact,
        test_chain_with_tool_result_gap,
        test_chain_unrelated_records_stay_separate,
        test_chain_three_hop,
        test_thinking_block_indices_only_signed,
        test_assistant_content_upto_blanks_thinking_and_truncates,
        test_sanitize_standalone_user_message_strips_tool_result_wrapper,
        test_sanitize_standalone_user_message_passthrough_for_plain_text,
        test_sanitize_standalone_user_message_handles_list_of_tool_results,
        test_last_user_message,
        test_looks_refused,
        test_large_block_defaults_to_length_only_with_reduced_attempts,
        test_large_block_sampled_confirms_strict_on_identical_repeat,
        test_large_block_near_miss_flagged_but_accepted,
        test_large_block_sampled_never_exceeds_tiered_call_budget,
        test_large_block_far_miss_not_flagged_near_miss,
        test_free_summary_text_reads_the_blocks_own_thinking_field,
        test_free_summary_extraction_costs_nothing_and_is_unverified,
        test_exact_length_match_not_discarded_as_refusal,
        test_generalized_near_miss_on_non_sampled_block,
        test_small_block_ignores_tiering,
    ]:
        fn()
    if _failures:
        print(f"\n{len(_failures)} FAILED: {_failures}")
        return 1
    print("\nALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
