#!/usr/bin/env python3
"""Batch, verified chain-of-thought extraction from an external agent harness's capture log.

Reads a JSONL capture log — one line per model call, each a `{route, model, request,
response}` object with the full request and reassembled response, covering both
native-Anthropic and Distill/Bedrock-style routes — as produced by a network tap sitting
between an agent framework's model-calling code and its upstream API (for example, a thin
proxy in front of an agent SDK's CLI subprocess). This is the shape you get for free from
any agentic coding harness (Claude Code-style CLIs, custom orchestrators, etc.) that
already logs raw API traffic, without needing a session id from the tap itself: chains are
recovered purely by matching how one call's `messages` extends a prior call's
`messages + response`.

Given such a log, this tool regroups the calls into per-session chains, replays every
`thinking` block's `signature` to recover the real (non-summarized) reasoning, verifies
each recovery locally, and writes:

  1. a flat extraction log (one line per thinking block: verification result, recovered
     text, sealed/recovered byte counts, sha256, notes) — `--out`
  2. reconstructed per-session trajectories with each thinking block's summarized/omitted
     text replaced by the verified recovery (falling back to the original block, tagged
     `verification: "none"`, when extraction did not succeed) — `--trajectories-out`

Why this is a separate tool from tools/extract_cot.py rather than a thin wrapper: that
tool harvests its own signatures from a single user-supplied question and replays with
just that question as context, which is the right shape for controlled experiments. This
tool consumes ALREADY-HARVESTED signatures from a live multi-agent pipeline (arbitrarily
deep tool-call history ahead of any given call) and has to decide what to replay
*against* — see `_REPLAY_PREFIX_STRATEGIES` below. Empirically (see docs/reproduction-log.md),
replaying with the full original multi-turn context did not help recovery and was refused
in testing, so the strategies here are cheap on purpose and never resend deep tool-call
history.

Verification levels and the length oracle are identical to tools/extract_cot.py — see
that file's docstring for the cryptographic argument. This tool imports its elicitor text
and length-oracle function from there so the two tools can never drift apart on wording.

Usage:
    set -a; source .env; set +a     # DISTILL_APP_ID/APP_KEY and/or ANTHROPIC_API_KEY

    python tools/extract_pipeline_capture.py \
        --capture-dir /path/to/_thinking_capture \
        --out logs/pipeline_extractions.jsonl \
        --trajectories-out logs/pipeline_trajectories.jsonl
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import server  # noqa: E402
from tools.extract_cot import VERBATIM_ELICITOR, sealed_length, UpstreamError, RETRY_STATUS  # noqa: E402

# A cheap, content-free retry prefix. See docs/reproduction-log.md §8.1: this was the ONLY
# prefix that recovered cleanly in that trial, while both the exact original context and
# an unrelated substantive turn were refused ("private scratchpad"). Kept as a fallback
# rather than the default because extract_cot.py's single-question approach (closest
# analogue here: the last real user-facing turn) already has much larger-sample validation
# in docs/reproduction-log.md.
_MINIMAL_PREFIX = [{"role": "user", "content": "Continue."}]

_REFUSAL_MARKERS = (
    "i can't", "i cannot", "i'm not going to", "i won't", "not going to produce",
    "not going to reproduce", "private scratchpad", "verbatim transcript of that",
    "don't have a faithful", "internal working notes",
)


# --------------------------------------------------------------------------------- #
# Loading + session-chain reconstruction
# --------------------------------------------------------------------------------- #
def load_records(capture_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    paths = sorted(capture_dir.glob("*.jsonl")) if capture_dir.is_dir() else [capture_dir]
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def _strip_cache_control(obj: Any) -> Any:
    """Drop `cache_control` breakpoint annotations before comparing message arrays.

    Real Claude Code traffic moves its (bounded number of) cache breakpoints forward as
    a conversation grows: the same message block that carried `cache_control` in one
    request often no longer carries it in a later one, even though the block's actual
    content is identical. Left in, this breaks the exact-match chaining below (found via
    a real pipeline run: a predecessor's reconstructed after-transcript differed from its
    successor's actual prefix ONLY in a `cache_control` key, at a fixed few-hundred-byte
    offset near the end of the shared prefix). Comparing chain identity must be
    content-based, not billing-annotation-based.
    """
    if isinstance(obj, dict):
        return {k: _strip_cache_control(v) for k, v in obj.items() if k != "cache_control"}
    if isinstance(obj, list):
        return [_strip_cache_control(v) for v in obj]
    return obj


def _canon(obj: Any) -> str:
    return json.dumps(_strip_cache_control(obj), sort_keys=False, ensure_ascii=False, separators=(",", ":"))


def _assistant_turn(record: dict[str, Any]) -> Optional[dict[str, Any]]:
    resp = record.get("response")
    if not resp or not isinstance(resp.get("content"), list):
        return None
    return {"role": "assistant", "content": resp["content"]}



# How many trailing messages a successor call may append beyond the exact point where it
# extends a predecessor's (messages + assistant turn) — e.g. one tool_result turn for a
# single tool_use, more for parallel tool calls, or an extra steered-in user message.
# Bounded rather than unbounded (checking every possible split) to keep chaining O(n) per
# record instead of O(messages) per record on long, hour-long auto-mode conversations.
_MAX_TRAILING_MESSAGES = 6


def chain_records(records: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group capture records into per-session chains.

    Record B follows record A iff some prefix of B's outbound `messages` reproduces A's
    outbound `messages` plus the assistant turn built from A's response, verbatim — B may
    append a bounded number of further messages after that point (typically one or more
    tool_result turns). This needs no session/agent id from the tap (it deliberately
    writes none — see thinking-capture.mjs): the growing message array across a real
    multi-turn conversation is itself a unique enough fingerprint. Records that don't
    extend anything start a new chain; a record with an unparseable response can still be
    chained (matched on messages alone) but can't be extended further.
    """
    usable = [r for r in records if isinstance(r.get("request", {}).get("messages"), list)]
    after_hash: dict[str, dict[str, Any]] = {}
    for r in usable:
        turn = _assistant_turn(r)
        if turn is None:
            continue
        full = r["request"]["messages"] + [turn]
        after_hash[_canon(full)] = r

    successor_of: dict[int, dict[str, Any]] = {}  # id(A) -> B
    for b in usable:
        msgs = b["request"]["messages"]
        for trailing in range(0, min(_MAX_TRAILING_MESSAGES, len(msgs) - 1) + 1):
            k = len(msgs) - trailing
            if k < 1:
                break
            a = after_hash.get(_canon(msgs[:k]))
            if a is not None and a is not b:
                successor_of[id(a)] = b
                break

    is_successor = {id(b) for b in successor_of.values()}
    chains: list[list[dict[str, Any]]] = []
    for r in usable:
        if id(r) in is_successor:
            continue  # not a chain head
        chain = [r]
        cur = r
        seen_ids = {id(r)}
        while id(cur) in successor_of:
            nxt = successor_of[id(cur)]
            if id(nxt) in seen_ids:  # defensive: never loop on malformed/duplicate logs
                break
            chain.append(nxt)
            seen_ids.add(id(nxt))
            cur = nxt
        chains.append(chain)
    # Stable order: by first record's timestamp, falling back to input order.
    chains.sort(key=lambda c: c[0].get("ts") or "")
    return chains


def thinking_block_indices(record: dict[str, Any]) -> list[int]:
    resp = record.get("response") or {}
    content = resp.get("content") or []
    return [i for i, b in enumerate(content) if b.get("type") == "thinking" and b.get("signature")]


def last_user_message(messages: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    for m in reversed(messages):
        if m.get("role") == "user":
            return m
    return None


def sanitize_standalone_user_message(msg: dict[str, Any]) -> dict[str, Any]:
    """Make a `messages[-1]`-style user turn safe to send as the FIRST message of a fresh
    replay conversation.

    Found on real pipeline data (2026-08-10 full run): when the real last user turn is
    itself a `tool_result` block (extremely common in a tool-using agent — e.g. right after
    a Read/Grep/Bash call), sending it standalone is rejected outright:
    `HTTP 400: unexpected tool_use_id found in tool_result blocks — each tool_result block
    must have a corresponding tool_use block in the previous message`. That failure was
    silent in the sense that it consumed zero of the attempt budget productively (an
    immediate 400, not a real elicitation attempt) while still occupying an attempt slot —
    on this run it was the single largest cause of `none`-verified LARGE blocks (multiple
    40-60 KB sealed traces recovered 0 bytes for exactly this reason).

    Fix: strip the `tool_result` wrapper and keep only its inner text, reframed as an
    ordinary user text turn — still gives the model *some* real prefix content (the tool
    output text) without violating the API's tool_use/tool_result pairing requirement.
    """
    content = msg.get("content")
    if not isinstance(content, list):
        return msg
    has_tool_result = any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
    if not has_tool_result:
        return msg
    parts: list[str] = []
    for b in content:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "tool_result":
            inner = b.get("content")
            if isinstance(inner, str):
                parts.append(inner)
            elif isinstance(inner, list):
                for ib in inner:
                    if isinstance(ib, dict) and ib.get("type") == "text":
                        parts.append(ib.get("text") or "")
        elif b.get("type") == "text":
            parts.append(b.get("text") or "")
    text = "\n".join(p for p in parts if p) or "Continue."
    return {"role": "user", "content": text}


def assistant_content_upto(content: list[dict[str, Any]], block_index: int) -> list[dict[str, Any]]:
    """Content blocks from index 0 through `block_index`, thinking text blanked (the API
    requires this — it must match the empty string the client would have received under
    display:omitted/summarized), truncated before a LATER, unrelated thinking block."""
    out: list[dict[str, Any]] = []
    for i in range(block_index + 1):
        b = content[i]
        if b.get("type") == "thinking":
            out.append({"type": "thinking", "thinking": "", "signature": b.get("signature")})
        else:
            out.append(b)
    return out


def looks_refused(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in _REFUSAL_MARKERS)


# --------------------------------------------------------------------------------- #
# Upstream calling — reuses server.py's endpoint/auth/wire-shape logic, retargeted per
# capture record's own route+model rather than the module's fixed env-derived globals,
# since one batch run can (and per the plan, should) mix native and Bedrock records.
# --------------------------------------------------------------------------------- #
@contextlib.contextmanager
def _route_context(route: str, model: str):
    prev = (
        server.UPSTREAM_MODE,
        server.UPSTREAM_PROVIDER,
        server.MODEL,
        server.UPSTREAM_BASE,
        server.UPSTREAM_KEY,
    )
    try:
        if route == "bedrock":
            server.UPSTREAM_MODE = "bedrock"
            server.UPSTREAM_PROVIDER = os.environ.get("UPSTREAM_PROVIDER", "aws_third")
            server.UPSTREAM_BASE = os.environ.get("UPSTREAM_BASE", "http://api.model-eval.woa.com").rstrip("/")
            app_id, app_key = os.environ.get("DISTILL_APP_ID", ""), os.environ.get("DISTILL_APP_KEY", "")
            if app_id and app_key:
                server.UPSTREAM_KEY = f"{app_id}:{app_key}"
        else:
            server.UPSTREAM_MODE = "anthropic"
            server.UPSTREAM_BASE = os.environ.get("NATIVE_UPSTREAM_BASE", "https://api.anthropic.com").rstrip("/")
            native_key = os.environ.get("ANTHROPIC_API_KEY", "")
            if native_key:
                server.UPSTREAM_KEY = native_key
        server.MODEL = model
        yield
    finally:
        (
            server.UPSTREAM_MODE,
            server.UPSTREAM_PROVIDER,
            server.MODEL,
            server.UPSTREAM_BASE,
            server.UPSTREAM_KEY,
        ) = prev


def call_upstream(route: str, model: str, body: dict[str, Any], label: str, *, retries: int = 5) -> dict[str, Any]:
    last: Optional[str] = None
    for attempt in range(1, retries + 1):
        try:
            with _route_context(route, model):
                data, cost = server._messages(server._finalize_upstream_body(body, model), label)
            return {"data": data, "cost": cost}
        except server.UpstreamError as e:
            last = str(e)
            if any(str(s) in last for s in RETRY_STATUS) and attempt < retries:
                time.sleep(min(20, 4 * attempt))
                continue
            raise UpstreamError(last) from e
    raise UpstreamError(last or "exhausted")


def response_text(data: dict[str, Any]) -> str:
    for b in data.get("content") or []:
        if b.get("type") == "text":
            return b.get("text") or ""
    return ""


# --------------------------------------------------------------------------------- #
# Per-block extraction
# --------------------------------------------------------------------------------- #
@dataclass
class BlockExtraction:
    chain_id: int
    turn_index: int
    block_index: int
    route: str
    model: str
    verified: bool
    verification: str  # strict | length | approximate | summary | none
    cot: str
    sealed_bytes: Optional[int]
    recovered_bytes: Optional[int]
    sha256: Optional[str]
    prefix_strategy: str
    attempts: int
    cost_usd: float
    is_large: bool = False
    near_miss: bool = False
    near_miss_diff: Optional[int] = None
    notes: list[str] = field(default_factory=list)


def free_summary_text(record: dict[str, Any], block_index: int) -> str:
    """The `thinking` field already sitting in a captured record, if the harvest request
    carried `thinking.display: "summarized"` — e.g. because the upstream proxy that produced
    this capture log rewrites the outbound `thinking` object before forwarding it. Billed
    identically to `display: "omitted"` (same single
    generation call, confirmed in Anthropic's own docs), so this text costs nothing beyond
    what harvest already paid for. It is a paraphrased, compressed summary, not the
    verbatim sealed reasoning — see `free_summary_extraction` below for why this is still
    the right default for most (small) blocks."""
    content = (record.get("response") or {}).get("content") or []
    if not (0 <= block_index < len(content)):
        return ""
    return content[block_index].get("thinking") or ""


def free_summary_extraction(
    *, chain_id: int, turn_index: int, block_index: int, record: dict[str, Any], target: Optional[int]
) -> BlockExtraction:
    """Zero-cost extraction using the free summarized thinking text already present in the
    capture record — no replay call. Used for blocks below `--free-summary-threshold-bytes`
    that already have non-empty free text (see the module docstring's cost analysis: on the
    2026-08-10 full-pipeline run, the free summary for SMALL blocks averaged 88% of the
    verbatim recovery's length and captured the same core decision — the marginal value of
    paying for byte-exact verbatim on a block that's already mostly a one-paragraph decision
    is low, while that exact same paid-extraction budget is worth far more on LARGE blocks,
    where the free summary is necessarily a tiny fraction of the actual reasoning)."""
    text = free_summary_text(record, block_index)
    route = record.get("route") or "native"
    model = record.get("model") or (record.get("request") or {}).get("model")
    return BlockExtraction(
        chain_id=chain_id, turn_index=turn_index, block_index=block_index, route=route, model=model,
        verified=False, verification="summary", cot=text, sealed_bytes=target,
        recovered_bytes=len(text.encode("utf-8")) if text else None, sha256=None,
        prefix_strategy="", attempts=0, cost_usd=0.0,
        notes=["free summarized thinking from harvest — no replay call, not length/byte verified"],
    )


def extract_block(
    *,
    chain_id: int,
    turn_index: int,
    record: dict[str, Any],
    block_index: int,
    max_tokens: int,
    attempts: int,
    verify: str,
    accept_within: int,
    large_threshold: int = 0,
    large_max_attempts: Optional[int] = None,
    near_miss_bytes: int = 100,
    do_strict_sample: bool = False,
) -> BlockExtraction:
    """Extract and verify one thinking block.

    Tiered policy for large sealed traces (`sealed_bytes >= large_threshold`): every
    `strict` verification round trips the FULL trace twice (once per independent replay),
    so cost scales with trace size. Large blocks default to `length`-only with a reduced
    attempt budget (`large_max_attempts`), and only a sampled subset (`do_strict_sample`,
    selected by the caller — see `main()`) pays for one additional confirmation replay:
      - digest matches the already-accepted text -> upgraded to `strict`.
      - digest differs but byte length is within `near_miss_bytes` of the accepted
        text -> still accepted at `length`, but flagged `near_miss` for manual review /
        an optional targeted re-run at `--verify strict`.
      - otherwise -> still accepted at `length`; the discrepancy is only noted.
    A large block that is NOT sampled never pays for the extra confirmation call at all.
    """
    route = record.get("route") or "native"
    model = record.get("model") or (record.get("request") or {}).get("model")
    content = (record.get("response") or {}).get("content") or []
    block = content[block_index]
    signature = block.get("signature")
    target = sealed_length(signature)
    messages = (record.get("request") or {}).get("messages") or []
    last_user = last_user_message(messages)
    assistant_content = assistant_content_upto(content, block_index)
    pending_tool_ids = [b["id"] for b in assistant_content if b.get("type") == "tool_use"]

    base = BlockExtraction(
        chain_id=chain_id, turn_index=turn_index, block_index=block_index, route=route,
        model=model, verified=False, verification="none", cot="", sealed_bytes=target,
        recovered_bytes=None, sha256=None, prefix_strategy="", attempts=0, cost_usd=0.0,
    )
    if target is None:
        base.notes.append("could not decode signature envelope")
        return base

    is_large = large_threshold > 0 and target >= large_threshold
    base.is_large = is_large
    # Cost fix (2026-08-11 full-batch run): `do_strict_sample` used to force the FULL
    # `verify`/`attempts` budget (strict tier, up to 4 tries) onto the main loop for
    # sampled blocks, instead of the cheap tiered path every other large block gets. On
    # real data this made the 5 sampled blocks average 4.4 attempts vs 2.1 for
    # non-sampled large blocks, at ~50% higher per-block cost, for a calibration signal
    # that was supposed to cost exactly one extra call. Sampling must never make a block
    # MORE expensive than the standard tiered path — it now always uses the same cheap
    # length-tier main loop as any other large block; `do_strict_sample` only controls
    # whether ONE extra confirmation replay (below) runs afterward.
    effective_verify = "length" if is_large else verify
    effective_attempts = (large_max_attempts or 2) if is_large else attempts
    if is_large:
        base.notes.append(
            f"large block (sealed={target} B >= threshold={large_threshold} B): "
            + ("length-tier, sampled for one extra confirmation replay" if do_strict_sample else "length-tier, not sampled")
        )

    def build_prefix(strategy: str) -> list[dict[str, Any]]:
        if strategy == "minimal":
            return copy.deepcopy(_MINIMAL_PREFIX)
        if last_user is None:
            return copy.deepcopy(_MINIMAL_PREFIX)
        return [copy.deepcopy(sanitize_standalone_user_message(last_user))]

    def one_attempt(strategy: str) -> tuple[str, float]:
        prefix = build_prefix(strategy)
        convo = prefix + [{"role": "assistant", "content": copy.deepcopy(assistant_content)}]
        if pending_tool_ids:
            convo.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": tid, "content": "(elided for extraction — not the original)"}
                        for tid in pending_tool_ids
                    ],
                }
            )
        convo.append({"role": "user", "content": VERBATIM_ELICITOR})
        out = call_upstream(route, model, {"max_tokens": max_tokens, "messages": convo}, "pipeline_extract_replay")
        return response_text(out["data"]), (out["cost"] or {}).get("usd", 0.0)

    # Strategy order: try the real preceding instruction first (larger-sample validated
    # elsewhere), then fall back to the content-free minimal prefix if that keeps refusing
    # or missing the length oracle — see the module docstring / docs/reproduction-log.md §8.1.
    strategies = ["last_user", "last_user", "minimal", "minimal"][:max(effective_attempts, 1)]
    seen: dict[str, str] = {}
    best: tuple[int, str, str] = (-1, "", "")
    closest: tuple[int, str, str, str] = (1 << 30, "", "", "")
    for i, strategy in enumerate(strategies, 1):
        try:
            text, cost = one_attempt(strategy)
        except UpstreamError as e:
            base.notes.append(f"attempt {i} ({strategy}) failed: {e}")
            continue
        base.cost_usd += cost
        base.attempts = i
        nbytes = len(text.encode("utf-8"))
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if nbytes > best[0]:
            best = (nbytes, text, strategy)

        # The length oracle is checked BEFORE the refusal heuristic on purpose. Found on
        # real pipeline data (2026-08-10 full run, 21/206 blocks): long, genuine agent
        # reasoning frequently contains ordinary English turns of phrase — "I can't just
        # do X, I need to..." — that coincidentally match `_REFUSAL_MARKERS`. That is
        # content, not a meta-refusal about the extraction request. An exact byte-length
        # match against `target` (independently decoded from the signature's ciphertext
        # length — see sealed_length) is cryptographic proof of a genuine recovery
        # regardless of what words appear in it; treating it as a "refusal" and discarding
        # it threw away 302 KB of correctly-recovered, highest-value (large/complex-task)
        # reasoning across this run for zero benefit. Only apply the refusal heuristic
        # below to attempts that did NOT already satisfy the length oracle.
        if nbytes == target:
            if effective_verify == "length":
                base.verified, base.verification, base.prefix_strategy = True, "length", strategy
                base.cot, base.recovered_bytes, base.sha256 = text, nbytes, digest
                break
            if digest in seen:
                base.verified, base.verification, base.prefix_strategy = True, "strict", strategy
                base.cot, base.recovered_bytes, base.sha256 = text, nbytes, digest
                break
            seen[digest] = text
            base.notes.append(f"attempt {i} ({strategy}): length-verified, awaiting a matching repeat")
            continue

        if looks_refused(text) and nbytes < target * 2:
            base.notes.append(f"attempt {i} ({strategy}): looks like a refusal ({nbytes} B)")
            continue
        delta = nbytes - target
        if abs(delta) < closest[0]:
            closest = (abs(delta), text, digest, strategy)
        base.notes.append(f"attempt {i} ({strategy}): {nbytes} B vs sealed {target} B (off by {delta})")

    if not base.verified:
        if seen:
            digest, text = next(iter(seen.items()))
            base.cot, base.sha256, base.verification = text, digest, "length"
        elif accept_within > 0 and closest[0] <= accept_within:
            base.cot, base.sha256, base.verification, base.prefix_strategy = closest[1], closest[2], "approximate", closest[3]
            base.notes.append(f"approximate: within {closest[0]} B of sealed length")
        elif closest[1] and closest[0] <= near_miss_bytes:
            # Generalized near-miss: no attempt hit the byte-exact length oracle, but the
            # CLOSEST attempt (not necessarily the longest — see `best` below) came within
            # `near_miss_bytes`. Found on real pipeline data: for very long (10-60 KB)
            # regenerations, the model occasionally reproduces content that is complete and
            # coherent but off by a handful of bytes (a dropped space, a rephrased word) —
            # not a content mismatch, most plausibly a token-level non-determinism in an
            # otherwise-genuine long regeneration. This is NOT byte-exact proof (kept out of
            # "length"/"strict"), but it is flagged `near_miss` for visibility and uses the
            # closest-by-length text rather than `best` (which tracks the LONGEST attempt
            # overall and can be a worse pick when a shorter attempt was actually nearer to
            # target).
            base.cot, base.sha256, base.prefix_strategy = closest[1], closest[2], closest[3]
            base.verification = "approximate"
            base.near_miss = True
            base.near_miss_diff = closest[0]
            base.notes.append(
                f"near-miss: closest attempt {closest[0]} B off sealed length "
                f"(<= {near_miss_bytes} B tolerance) — flagged for review, not byte-exact"
            )
        else:
            base.cot, base.prefix_strategy = best[1], best[2]
            if base.cot:
                base.sha256 = hashlib.sha256(base.cot.encode("utf-8")).hexdigest()
                base.notes.append("unverified: returning the longest attempt")
        base.recovered_bytes = len(base.cot.encode("utf-8")) if base.cot else None
        rank = {"none": 0, "approximate": 1, "length": 2, "strict": 3}
        base.verified = rank[base.verification] >= rank[verify]

    # Sampled strict confirmation for a large, length-only-accepted block: one extra
    # replay, purely for calibration — never required for the block to be accepted.
    if is_large and do_strict_sample and base.verification == "length" and base.cot:
        try:
            extra_text, extra_cost = one_attempt("last_user")
            base.cost_usd += extra_cost
            base.attempts += 1
            extra_bytes = len(extra_text.encode("utf-8"))
            extra_digest = hashlib.sha256(extra_text.encode("utf-8")).hexdigest()
            if extra_digest == base.sha256:
                base.verification = "strict"
                base.notes.append("sampled confirmation replay: byte-identical — upgraded to strict")
            else:
                diff = abs(extra_bytes - base.recovered_bytes)
                if diff <= near_miss_bytes:
                    base.near_miss = True
                    base.near_miss_diff = diff
                    base.notes.append(
                        f"sampled confirmation replay: different SHA256 but only {diff} B apart "
                        f"({extra_bytes} B vs {base.recovered_bytes} B) — near-miss, consider "
                        f"re-running this block with --verify strict"
                    )
                else:
                    base.notes.append(
                        f"sampled confirmation replay: diverged ({extra_bytes} B vs "
                        f"{base.recovered_bytes} B accepted, diff {diff} B) — kept at length"
                    )
        except UpstreamError as e:
            base.notes.append(f"sampled confirmation replay failed: {e}")
    return base


# --------------------------------------------------------------------------------- #
# Trajectory reconstruction
# --------------------------------------------------------------------------------- #
def build_trajectories(
    chains: list[list[dict[str, Any]]],
    extractions_by_key: dict[tuple[int, int, int], BlockExtraction],
) -> list[dict[str, Any]]:
    trajectories = []
    for chain_id, chain in enumerate(chains):
        turns = []
        for turn_index, record in enumerate(chain):
            resp = copy.deepcopy(record.get("response") or {})
            content = resp.get("content") or []
            verifications = []
            for block_index, block in enumerate(content):
                if block.get("type") != "thinking":
                    continue
                key = (chain_id, turn_index, block_index)
                ext = extractions_by_key.get(key)
                if ext is None:
                    verifications.append({"block_index": block_index, "verification": "none"})
                    continue
                if ext.cot:
                    block["thinking"] = ext.cot  # verified (or best-effort) real reasoning
                verifications.append(
                    {
                        "block_index": block_index,
                        "verification": ext.verification,
                        "sealed_bytes": ext.sealed_bytes,
                        "recovered_bytes": ext.recovered_bytes,
                        "sha256": ext.sha256,
                    }
                )
            turns.append(
                {
                    "route": record.get("route"),
                    "model": record.get("model"),
                    "request": record.get("request"),
                    "response": resp,
                    "thinking_verifications": verifications,
                }
            )
        trajectories.append({"chain_id": chain_id, "num_turns": len(turns), "turns": turns})
    return trajectories


def index_extractions_by_signature(
    chains: list[list[dict[str, Any]]], extractions_by_signature: dict[str, "BlockExtraction"]
) -> dict[tuple[int, int, int], "BlockExtraction"]:
    """Map a signature-keyed extraction store onto (chain_id, turn_index, block_index)
    positions for a given `chains` grouping. Used by the streaming worker, where chain/turn
    positions are only stable for a single `chain_records()` call, but extractions are
    accumulated incrementally and keyed by the one thing that never changes: the signature
    itself.
    """
    out: dict[tuple[int, int, int], BlockExtraction] = {}
    for chain_id, chain in enumerate(chains):
        for turn_index, record in enumerate(chain):
            for block_index in thinking_block_indices(record):
                sig = record["response"]["content"][block_index].get("signature")
                ext = extractions_by_signature.get(sig)
                if ext is not None:
                    out[(chain_id, turn_index, block_index)] = ext
    return out


# --------------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------------- #
def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--capture-dir", type=Path, required=True, help="dir of *.jsonl capture files, or a single file")
    ap.add_argument("--out", type=Path, required=True, help="flat per-block extraction log (JSONL)")
    ap.add_argument("--trajectories-out", type=Path, default=None, help="reconstructed per-session trajectories (JSONL)")
    ap.add_argument("--verify", choices=["strict", "length", "approximate"], default="strict")
    ap.add_argument("--accept-within", type=int, default=0, metavar="N")
    ap.add_argument("--max-tokens", type=int, default=64000)
    ap.add_argument("--attempts", type=int, default=4)
    ap.add_argument("--max-blocks", type=int, default=None, help="cap total signatures replayed (cost control)")
    ap.add_argument("--model-filter", default=None, help="regex; only replay blocks whose model matches")
    ap.add_argument(
        "--large-threshold-bytes", type=int, default=4000, metavar="N",
        help="sealed_bytes at/above this size use the tiered (length-only + sampled strict) "
             "policy instead of full --verify on every attempt; 0 disables tiering entirely",
    )
    ap.add_argument(
        "--large-max-attempts", type=int, default=2, metavar="N",
        help="attempt budget for a large block (length tier — cheaper than --attempts). Costed "
             "out against the 2026-08-10 full-pipeline run: a 3rd attempt cost $84.72 across 25 "
             "blocks for only 10 (40%%) additional verifications, i.e. a very poor cost/yield "
             "trade concentrated in the largest (>=35 KB) blocks. 2 attempts captures nearly all "
             "of the recoverable value at a fraction of the cost.",
    )
    ap.add_argument(
        "--strict-sample-count", type=int, default=3, metavar="N",
        help="how many large blocks (first-encountered, deterministic) get one extra sampled "
             "confirmation replay for calibration only (does not gate accept/reject — see the "
             "cost fix in extract_block: sampling no longer forces the expensive strict-tier "
             "budget). 3 is enough to sanity-check the length oracle's miss rate per run.",
    )
    ap.add_argument(
        "--near-miss-bytes", type=int, default=100, metavar="N",
        help="a sampled large block whose confirmation replay differs by at most this many bytes "
             "(but has a different SHA256) is accepted at 'length' and flagged near_miss for review",
    )
    ap.add_argument(
        "--near-miss-out", type=Path, default=None,
        help="near-miss extractions (JSONL), default: <out>.near_miss.jsonl",
    )
    ap.add_argument(
        "--free-summary-threshold-bytes", type=int, default=4000, metavar="N",
        help="blocks below this sealed size that already have non-empty free summarized "
             "thinking text (from thinking.display=summarized at harvest time, e.g. injected "
             "by an upstream capture proxy) are NOT replayed at all; the "
             "free text is used as-is (verification='summary', cost $0). Costed out against "
             "the 2026-08-10 run: paid extraction of blocks below this threshold cost $39.42 "
             "for text that averaged 88%% of the verbatim length and captured the same core "
             "decision — low marginal value versus spending that budget on large blocks, "
             "where the free summary is a small fraction of the actual reasoning. Set to 0 "
             "to always pay for verbatim extraction (the old behavior); set higher than "
             "--large-threshold-bytes to also skip free-summary blocks that happen to sit "
             "just below the large tier. A block with NO free summary text (e.g. harvested "
             "before the injectThinkingDisplay absent-thinking-key fix) is always replayed "
             "regardless of this flag, since there is nothing free to fall back to.",
    )
    args = ap.parse_args(argv)

    import re

    records = load_records(args.capture_dir)
    chains = chain_records(records)
    total_blocks = sum(len(thinking_block_indices(r)) for c in chains for r in c)
    print(f"[extract] {len(records)} records -> {len(chains)} chains, {total_blocks} thinking blocks with a signature", file=sys.stderr)

    model_re = re.compile(args.model_filter) if args.model_filter else None

    # First pass (no API calls — sealed_length is decoded locally from the signature):
    # classify every candidate block as large/not, and deterministically pick which large
    # blocks get the sampled strict-confirmation replay (first N encountered, in processing
    # order, so a re-run with the same capture log samples the same blocks).
    candidates: list[tuple[int, int, dict[str, Any], int]] = []
    for chain_id, chain in enumerate(chains):
        for turn_index, record in enumerate(chain):
            model = record.get("model") or ""
            if model_re and not model_re.search(model):
                continue
            for block_index in thinking_block_indices(record):
                candidates.append((chain_id, turn_index, record, block_index))

    def block_sealed_bytes(chain_id: int, turn_index: int, record: dict[str, Any], block_index: int) -> Optional[int]:
        sig = record["response"]["content"][block_index].get("signature")
        return sealed_length(sig)

    sample_budget = args.strict_sample_count
    do_sample_for: set[tuple[int, int, int]] = set()
    if args.large_threshold_bytes > 0:
        for chain_id, turn_index, record, block_index in candidates:
            if sample_budget <= 0:
                break
            sealed = block_sealed_bytes(chain_id, turn_index, record, block_index)
            if sealed is not None and sealed >= args.large_threshold_bytes:
                do_sample_for.add((chain_id, turn_index, block_index))
                sample_budget -= 1

    extractions: list[BlockExtraction] = []
    extractions_by_key: dict[tuple[int, int, int], BlockExtraction] = {}
    near_miss: list[BlockExtraction] = []
    done = 0
    for chain_id, turn_index, record, block_index in candidates:
        if args.max_blocks is not None and done >= args.max_blocks:
            break
        done += 1
        model = record.get("model") or ""
        sealed = block_sealed_bytes(chain_id, turn_index, record, block_index)
        has_free_summary = bool(free_summary_text(record, block_index))
        use_free_summary = (
            has_free_summary
            and sealed is not None
            and sealed < args.free_summary_threshold_bytes
        )
        if use_free_summary:
            ext = free_summary_extraction(
                chain_id=chain_id, turn_index=turn_index, block_index=block_index, record=record, target=sealed,
            )
            print(
                f"[extract] chain={chain_id} turn={turn_index} block={block_index} model={model}: "
                f"free summary ({sealed} B sealed, skipped — below --free-summary-threshold-bytes)",
                file=sys.stderr,
            )
        else:
            print(f"[extract] chain={chain_id} turn={turn_index} block={block_index} model={model} ...", file=sys.stderr)
            ext = extract_block(
                chain_id=chain_id, turn_index=turn_index, record=record, block_index=block_index,
                max_tokens=args.max_tokens, attempts=args.attempts, verify=args.verify,
                accept_within=args.accept_within, large_threshold=args.large_threshold_bytes,
                large_max_attempts=args.large_max_attempts, near_miss_bytes=args.near_miss_bytes,
                do_strict_sample=(chain_id, turn_index, block_index) in do_sample_for,
            )
        extractions.append(ext)
        extractions_by_key[(chain_id, turn_index, block_index)] = ext
        if ext.near_miss:
            near_miss.append(ext)
        print(
            f"    verified={ext.verified} ({ext.verification}) sealed={ext.sealed_bytes} "
            f"recovered={ext.recovered_bytes} attempts={ext.attempts} strategy={ext.prefix_strategy} "
            f"large={ext.is_large} near_miss={ext.near_miss} ${ext.cost_usd:.4f}",
            file=sys.stderr,
        )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as fh:
        for ext in extractions:
            fh.write(json.dumps(asdict(ext), ensure_ascii=False) + "\n")
    print(f"[extract] wrote {len(extractions)} extractions to {args.out}", file=sys.stderr)

    if args.trajectories_out:
        trajectories = build_trajectories(chains, extractions_by_key)
        args.trajectories_out.parent.mkdir(parents=True, exist_ok=True)
        with args.trajectories_out.open("w", encoding="utf-8") as fh:
            for traj in trajectories:
                fh.write(json.dumps(traj, ensure_ascii=False) + "\n")
        print(f"[extract] wrote {len(trajectories)} trajectories to {args.trajectories_out}", file=sys.stderr)

    if near_miss:
        near_miss_out = args.near_miss_out or args.out.with_suffix(".near_miss.jsonl")
        near_miss_out.parent.mkdir(parents=True, exist_ok=True)
        with near_miss_out.open("w", encoding="utf-8") as fh:
            for ext in near_miss:
                fh.write(json.dumps(asdict(ext), ensure_ascii=False) + "\n")
        print(f"[extract] wrote {len(near_miss)} near-miss extraction(s) to {near_miss_out}", file=sys.stderr)
        print(
            f"[extract] NOTE: {len(near_miss)} large block(s) had a sampled confirmation replay "
            f"that was close (<= {args.near_miss_bytes} B) but not byte-identical. They were "
            f"still accepted at 'length'. Consider re-running just these "
            f"(chain_id, turn_index, block_index) with --verify strict to confirm: "
            + ", ".join(f"({e.chain_id},{e.turn_index},{e.block_index})" for e in near_miss),
            file=sys.stderr,
        )

    ok = sum(1 for e in extractions if e.verified)
    large_count = sum(1 for e in extractions if e.is_large)
    free_count = sum(1 for e in extractions if e.verification == "summary")
    covered = sum(1 for e in extractions if e.cot)
    print(
        f"\nverified {ok}/{len(extractions)} (byte-exact) · {free_count} free-summary (no replay, $0) "
        f"· {covered}/{len(extractions)} have SOME thinking text (full trajectory coverage) · "
        f"{large_count} large (>= {args.large_threshold_bytes} B) · {len(near_miss)} near-miss · "
        f"total ${sum(e.cost_usd for e in extractions):.4f}",
        file=sys.stderr,
    )
    return 0 if (not extractions or ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
