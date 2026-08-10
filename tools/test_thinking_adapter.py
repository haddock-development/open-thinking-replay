#!/usr/bin/env python3
"""Unit tests for the model-capability thinking adapter (Opus 4.8 / Fable 5 / manual).

Runnable with plain python (no pytest required):

    cd open-thinking-replay
    python tools/test_thinking_adapter.py

Sets a bedrock-mode env *before* importing server so module-level config resolves
to the Distill/Bedrock code paths, then exercises the pure body-construction and
error-handling helpers without hitting the network (except one monkeypatched call).
"""
from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Configure bedrock mode deterministically before importing server.
os.environ.update(
    {
        "UPSTREAM_MODE": "bedrock",
        "UPSTREAM_BASE": "http://api.model-eval.woa.com",
        "UPSTREAM_PROVIDER": "aws_third",
        "DISTILL_APP_ID": "TEST_APP",
        "DISTILL_APP_KEY": "TEST_KEY",
        "MODEL": "anthropic.claude-sonnet-4-5-20250929-v1:0",
        "THINKING_MODE": "auto",
        "EFFORT": "high",
        "THINKING_DISPLAY": "omitted",
        "THINKING_BUDGET": "8000",
        "MAX_TOKENS": "16000",
    }
)
# Drop any real credentials that a sourced .env may have exported into this shell.
for _k in ("UPSTREAM_KEY", "ANTHROPIC_API_KEY", "DISTILL_MODEL_MARKER"):
    os.environ.pop(_k, None)

import server  # noqa: E402

OPUS48 = "anthropic.claude-opus-4-8"
OPUS5 = "anthropic.claude-opus-5"
FABLE5 = "anthropic.claude-fable-5"
SONNET45 = "anthropic.claude-sonnet-4-5-20250929-v1:0"

_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" :: {detail}" if detail and not cond else ""))
    if not cond:
        _failures.append(name)


def test_profiles() -> None:
    check("profile_opus48_adaptive", server.thinking_profile(OPUS48) == "adaptive",
          server.thinking_profile(OPUS48))
    check("profile_fable5_always_on", server.thinking_profile(FABLE5) == "always_on_adaptive",
          server.thinking_profile(FABLE5))
    check("profile_sonnet45_manual", server.thinking_profile(SONNET45) == "manual",
          server.thinking_profile(SONNET45))
    check("profile_opus47_adaptive", server.thinking_profile("anthropic.claude-opus-4-7") == "adaptive")
    # Opus 5 rejects manual enabled+budget_tokens (verified live on Distill aws_third).
    check("profile_opus5_adaptive", server.thinking_profile(OPUS5) == "adaptive",
          server.thinking_profile(OPUS5))
    check("profile_sonnet5_adaptive", server.thinking_profile("anthropic.claude-sonnet-5") == "adaptive")


def test_harvest_opus5_no_manual_budget() -> None:
    """Opus 5 must never receive thinking.type=enabled (Distill returns HTTP 400)."""
    body = server._harvest_body(OPUS5, [{"role": "user", "content": "hi"}], effort="max")
    th = body.get("thinking") or {}
    check("opus5_thinking_adaptive", th.get("type") == "adaptive", str(th))
    check("opus5_no_budget", "budget_tokens" not in th, str(th))
    check("opus5_effort_max", (body.get("output_config") or {}).get("effort") == "max")


def test_harvest_opus48() -> None:
    body = server._harvest_body(OPUS48, [{"role": "user", "content": "hi"}],
                                max_tokens=16000, effort="max")
    th = body.get("thinking") or {}
    check("opus48_thinking_adaptive", th.get("type") == "adaptive", str(th))
    check("opus48_no_budget", "budget_tokens" not in th, str(th))
    check("opus48_effort_max", (body.get("output_config") or {}).get("effort") == "max", str(body.get("output_config")))
    check("opus48_has_display", "display" in th, str(th))


def test_harvest_fable5() -> None:
    body = server._harvest_body(FABLE5, [{"role": "user", "content": "hi"}], effort="max")
    th = body.get("thinking") or {}
    check("fable5_thinking_adaptive", th.get("type") == "adaptive", str(th))
    check("fable5_no_budget", "budget_tokens" not in th, str(th))
    check("fable5_effort_max", (body.get("output_config") or {}).get("effort") == "max")


def test_harvest_manual() -> None:
    body = server._harvest_body(SONNET45, [{"role": "user", "content": "hi"}],
                                max_tokens=4096, thinking_budget=2048)
    th = body.get("thinking") or {}
    check("sonnet45_thinking_enabled", th.get("type") == "enabled", str(th))
    check("sonnet45_budget_present", th.get("budget_tokens") == 2048, str(th))
    check("sonnet45_no_output_config", "output_config" not in body, str(body.keys()))


def test_finalize_keeps_adaptive_and_effort() -> None:
    body = server._harvest_body(OPUS48, [{"role": "user", "content": "hi"}], effort="max")
    final = server._finalize_upstream_body(dict(body), OPUS48)
    th = final.get("thinking") or {}
    check("finalize_drops_model", "model" not in final, str(final.keys()))
    check("finalize_keeps_output_config", (final.get("output_config") or {}).get("effort") == "max")
    check("finalize_keeps_adaptive", th.get("type") == "adaptive", str(th))
    check("finalize_no_budget_injected", "budget_tokens" not in th, str(th))
    check("finalize_adds_anthropic_version",
          final.get("anthropic_version") == server.BEDROCK_ANTHROPIC_VERSION)


def test_finalize_manual_max_tokens() -> None:
    body = server._harvest_body(SONNET45, [{"role": "user", "content": "hi"}],
                                max_tokens=1000, thinking_budget=8000)
    final = server._finalize_upstream_body(dict(body), SONNET45)
    check("finalize_manual_max_gt_budget", final["max_tokens"] > 8000, str(final.get("max_tokens")))


def test_auth_marker_separation() -> None:
    # Without marker: query model equals the endpoint model id.
    headers = server._auth_headers(OPUS48)
    check("auth_default_model_in_key", f"model={OPUS48}" in headers["x-api-key"], headers["x-api-key"])
    check("endpoint_uses_model", server._endpoint_for(OPUS48).endswith(f"/model/{OPUS48}/invoke"))
    # With marker override: auth query uses marker, endpoint still uses the real id.
    server.DISTILL_MODEL_MARKER = "api_anthropic_claude-fable-5"
    try:
        headers2 = server._auth_headers(FABLE5)
        check("auth_marker_in_key", "model=api_anthropic_claude-fable-5" in headers2["x-api-key"],
              headers2["x-api-key"])
        check("endpoint_ignores_marker", server._endpoint_for(FABLE5).endswith(f"/model/{FABLE5}/invoke"))
    finally:
        server.DISTILL_MODEL_MARKER = ""


def test_bedrock_wire_default() -> None:
    check("wire_bedrock_for_aws_provider", server._wire_shape() == "bedrock",
          server._wire_shape())


def test_native_distill_wire() -> None:
    # provider=anthropic on a Distill gateway → native /v1/messages wire, Distill auth.
    saved = server.UPSTREAM_PROVIDER
    server.UPSTREAM_PROVIDER = "anthropic"
    try:
        check("wire_native_for_anthropic_provider", server._wire_shape() == "native",
              server._wire_shape())
        ep = server._endpoint_for(FABLE5)
        check("native_endpoint_v1messages", ep.endswith("/v1/messages"), ep)
        h = server._auth_headers(FABLE5)
        check("native_distill_key_has_provider", "provider=anthropic" in h["x-api-key"], h["x-api-key"])
        check("native_distill_has_anthropic_version", "anthropic-version" in h, str(h))
        body = server._harvest_body(FABLE5, [{"role": "user", "content": "x"}], effort="max")
        final = server._finalize_upstream_body(dict(body), FABLE5)
        check("native_keeps_model", final.get("model") == FABLE5, str(final.keys()))
        check("native_no_bedrock_version", "anthropic_version" not in final,
              str(final.get("anthropic_version")))
        check("native_keeps_effort", (final.get("output_config") or {}).get("effort") == "max")
        check("native_keeps_adaptive", (final.get("thinking") or {}).get("type") == "adaptive")
    finally:
        server.UPSTREAM_PROVIDER = saved


class _FakeResp:
    def __init__(self, status_code, payload, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or str(payload)

    def json(self):
        return self._payload


def test_messages_raises_on_error_message() -> None:
    original = server.requests.post
    # 1) HTTP 400 + top-level {"message": ...} (Distill validation error shape)
    server.requests.post = lambda *a, **k: _FakeResp(
        400, {"message": '"thinking.type.enabled" is not supported for this model.'}
    )
    raised = False
    try:
        server._messages({"model": OPUS48, "max_tokens": 8, "messages": []}, label="test_err")
    except server.UpstreamError:
        raised = True
    finally:
        server.requests.post = original
    check("messages_raises_on_http400_message", raised)

    # 2) HTTP 200 + {"message": ...} without content must also raise.
    server.requests.post = lambda *a, **k: _FakeResp(200, {"message": "no available account"})
    raised2 = False
    try:
        server._messages({"model": FABLE5, "max_tokens": 8, "messages": []}, label="test_err2")
    except server.UpstreamError:
        raised2 = True
    finally:
        server.requests.post = original
    check("messages_raises_on_200_message_no_content", raised2)


def test_messages_accepts_valid() -> None:
    original = server.requests.post
    server.requests.post = lambda *a, **k: _FakeResp(
        200,
        {
            "model": "claude-opus-4-8",
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
            "stop_reason": "end_turn",
        },
    )
    ok = False
    try:
        data, cost = server._messages({"model": OPUS48, "max_tokens": 8, "messages": []},
                                      label="test_ok")
        ok = data.get("content")[0]["text"] == "ok" and "usd" in cost
    finally:
        server.requests.post = original
    check("messages_accepts_valid_response", ok)


def test_sealed_length_oracle() -> None:
    """The verifier in extract_cot must read the sealed length locally from a signature."""
    import base64

    from tools import extract_cot

    def varint(n: int) -> bytes:
        out = bytearray()
        while True:
            b = n & 0x7F
            n >>= 7
            out.append(b | 0x80 if n else b)
            if not n:
                return bytes(out)

    def envelope(ciphertext: bytes) -> str:
        def ld(field_no: int, payload: bytes) -> bytes:
            return varint(field_no << 3 | 2) + varint(len(payload)) + payload

        header = ld(6, b"claude-test") + ld(8, b"thinking")
        inner = ld(1, header) + ld(2, b"\x00" * 12) + ld(5, ciphertext)
        return base64.b64encode(ld(2, inner)).decode()

    for plaintext_len in (0, 1, 47, 120):
        sig = envelope(b"\x11" * (plaintext_len + extract_cot.GCM_TAG_BYTES))
        got = extract_cot.sealed_length(sig)
        check(f"sealed_length_{plaintext_len}", got == plaintext_len, f"got {got}")

    check("sealed_length_rejects_garbage", extract_cot.sealed_length("not-a-signature") is None)
    check("gcm_tag_is_16", extract_cot.GCM_TAG_BYTES == 16)
    # The elicitor must stay fixed: varying it between attempts breaks the determinism check.
    check("elicitor_has_no_placeholders",
          "{" not in extract_cot.VERBATIM_ELICITOR and "%" not in extract_cot.VERBATIM_ELICITOR)


def main() -> int:
    print(f"server UPSTREAM_MODE={server.UPSTREAM_MODE}")
    for fn in (
        test_profiles,
        test_sealed_length_oracle,
        test_harvest_opus5_no_manual_budget,
        test_harvest_opus48,
        test_harvest_fable5,
        test_harvest_manual,
        test_finalize_keeps_adaptive_and_effort,
        test_finalize_manual_max_tokens,
        test_bedrock_wire_default,
        test_native_distill_wire,
        test_auth_marker_separation,
        test_messages_raises_on_error_message,
        test_messages_accepts_valid,
    ):
        try:
            fn()
        except Exception:
            _failures.append(fn.__name__)
            print(f"[ERROR] {fn.__name__}")
            traceback.print_exc()
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}): {', '.join(_failures)}")
        return 1
    print("ALL TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
