#!/usr/bin/env python3
"""Continuous, decoupled background worker for extracting verified thinking from a live
agent-harness capture log (see tools/extract_pipeline_capture.py's docstring for the log
shape), without touching harvest speed at all.

Why a separate worker instead of "batch after one harvest run finishes": harvest calls
within a single pipeline run are strictly sequential (each depends on the prior
conversation state); replay calls for different signatures are fully independent of each
other and of harvest. A continuously-polling, independent process lets many concurrent
replay calls proceed in parallel while one or more harvest sessions keep running
elsewhere, with zero coupling — harvest never waits on this process, and a bug here can
never break a live turn (it only reads capture files; it never touches the pipeline).

Account isolation: by default this worker points its OWN process environment at a
dedicated Distill account (`WORKER_DISTILL_APP_ID` / `WORKER_DISTILL_APP_KEY`), so replay
traffic never shares a rate-limit / account-rotation bucket with harvest traffic (which
runs in a different process with its own environment either way). If no worker-specific
credentials are set, it falls back to the same `DISTILL_APP_ID` / `DISTILL_APP_KEY`
harvest uses.

Resumable: a JSON checkpoint (`<out-dir>/worker_checkpoint.json`) tracks a byte offset per
watched file and a set of already-processed signature hashes, so a restart never re-pays
for a signature already extracted, and never double-counts the sampled-strict budget
(`--strict-sample-count` is a *global*, checkpoint-persisted count across the whole
worker's lifetime, not per poll cycle).

Usage:
    set -a; source .env; set +a   # DISTILL_APP_ID/KEY (harvest) + optionally WORKER_DISTILL_*
    python tools/thinking_extract_worker.py \
        --watch-dir /path/to/_thinking_capture \
        --out-dir logs/worker_out

    # one-shot (drain whatever is currently in the capture dir, then exit) — for testing
    # the worker's own logic, or for a simple "run once after harvest is done" batch mode
    # using the exact same code path as the continuous worker:
    python tools/thinking_extract_worker.py --watch-dir ... --out-dir ... --once
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.extract_pipeline_capture import (  # noqa: E402
    BlockExtraction,
    build_trajectories,
    chain_records,
    extract_block,
    free_summary_extraction,
    free_summary_text,
    index_extractions_by_signature,
    sealed_length,
    thinking_block_indices,
)

# Replay calls are fully independent of each other (unlike harvest, which is strictly
# sequential per conversation) and of the harvest process, so concurrency here is a pure
# throughput/rate-limit tradeoff. Was raised 6 -> 8 on the 2026-08-10 run's latency profile
# (see below); correction from a later real run: at 8, a 106-block backlog batch hit the
# gateway's documented per-account cap ("current request rate has exceeded the AIHub
# platform's user limit (limit: 20 requests/minute)") almost immediately, and 55/106 blocks
# failed on every attempt as a result — this is a hard account-level ceiling, not a
# transient 429 that backs off and clears, so concurrency has to stay under it regardless
# of latency headroom. Dropped to 3, plus RequestRateLimiter below enforces the 20/minute
# ceiling directly rather than hoping concurrency alone keeps issuance under it.
DEFAULT_CONCURRENCY = 3
DEFAULT_MAX_REQUESTS_PER_MINUTE = 15  # stay under the observed 20/min account cap with margin
DEFAULT_POLL_INTERVAL = 5.0
DEFAULT_LARGE_THRESHOLD_BYTES = 4000
# Re-tuned 2->3->2 after actually costing out the completed 2026-08-10 batch run
# ($151.56 / 206 blocks). Per-block cost scales with output size regardless of tier, so
# an attempt that DOESN'T hit the length oracle is not a cheap no-op — it is a full-price
# regeneration of that block's entire content. Breaking down the run's own data by
# attempts-used showed why 3 was a bad trade: blocks that succeeded on attempt 1 cost
# $0.47/block on average; blocks that exhausted all 3 attempts cost $84.72 for just 25
# blocks (avg $3.39/block) and only 10 of those 25 (40%) ever verified — i.e. the 3rd
# attempt's marginal cost was wildly disproportionate to its marginal yield, concentrated
# in the very largest (>=35 KB) blocks where a 3rd try succeeded only once in this whole
# run. (Separately, ~$27 of this run's cost was attempts 2-3 wasted on blocks whose
# ALREADY-CORRECT attempt-1 text was misclassified as a refusal — that bug is now fixed,
# see extract_pipeline_capture.py, so most of what used to need attempt 2-3 will now
# resolve on attempt 1 automatically. 2 is enough for what remains.)
DEFAULT_LARGE_MAX_ATTEMPTS = 2
# Sampling no longer changes accept/reject (see the cost fix in extract_block: a sampled
# large block now runs the SAME cheap length-tier main loop as any other large block, and
# pays for exactly one extra confirmation replay for calibration) — so raising this only
# adds cost with no effect on the verified rate. 3 is enough of a calibration sample to
# see whether the length oracle is behaving on a given run.
DEFAULT_STRICT_SAMPLE_COUNT = 3
DEFAULT_NEAR_MISS_BYTES = 100
DEFAULT_ATTEMPTS = 4
DEFAULT_MAX_TOKENS = 64000
DEFAULT_REBUILD_EVERY = 10  # rebuild trajectories.jsonl every N newly completed extractions
# Below this sealed size, a block that already has non-empty free summarized thinking text
# (thinking.display=summarized at harvest, billed the same as omitted — e.g. injected by an
# upstream capture proxy) is never replayed at all; see
# extract_pipeline_capture.free_summary_extraction for the cost/quality rationale. This was
# by far the single biggest lever found on a real batch run: paid extraction of blocks
# below this threshold cost $39.42 for text that averaged 88% of the verbatim length and
# captured the same core decision, while contributing nothing towards recovering the ~90%
# of total reasoning content concentrated in the large blocks above this threshold.
DEFAULT_FREE_SUMMARY_THRESHOLD_BYTES = 4000


def configure_worker_account() -> None:
    """Point THIS process's Distill credentials at a dedicated worker account when one is
    configured. Only affects this process's own environment — harvest runs as a separate
    process with its own environment either way, so this never mutates what harvest uses.
    """
    dedicated = bool(os.environ.get("WORKER_DISTILL_APP_ID"))
    app_id = os.environ.get("WORKER_DISTILL_APP_ID") or os.environ.get("DISTILL_APP_ID", "")
    app_key = os.environ.get("WORKER_DISTILL_APP_KEY") or os.environ.get("DISTILL_APP_KEY", "")
    os.environ["DISTILL_APP_ID"] = app_id
    os.environ["DISTILL_APP_KEY"] = app_key
    print(
        "[worker] Distill account: "
        + ("dedicated worker account (WORKER_DISTILL_APP_ID)" if dedicated else "shared with harvest (no WORKER_DISTILL_APP_ID set)"),
        file=sys.stderr,
    )
    if not app_id or not app_key:
        print("[worker] WARNING: no Distill credentials resolved (neither WORKER_DISTILL_APP_ID nor DISTILL_APP_ID set)", file=sys.stderr)


# --------------------------------------------------------------------------------- #
# Checkpoint — survives restarts; a signature already processed is never re-paid for.
# --------------------------------------------------------------------------------- #
class Checkpoint:
    def __init__(self, path: Path):
        self.path = path
        self.file_offsets: dict[str, int] = {}
        self.seen_signature_hashes: set[str] = set()
        self.sample_used = 0
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        self.file_offsets = data.get("file_offsets", {})
        self.seen_signature_hashes = set(data.get("seen_signature_hashes", []))
        self.sample_used = data.get("sample_used", 0)

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "file_offsets": self.file_offsets,
                    "seen_signature_hashes": sorted(self.seen_signature_hashes),
                    "sample_used": self.sample_used,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        tmp.replace(self.path)


def read_new_complete_lines(path: Path, offset: int) -> tuple[list[str], int]:
    """Read whole new lines appended since `offset`. Never commits a partial trailing line
    (one not yet terminated by a newline) — it is re-read, complete, on the next poll."""
    with path.open("rb") as f:
        f.seek(offset)
        data = f.read()
    if not data:
        return [], offset
    last_nl = data.rfind(b"\n")
    if last_nl == -1:
        return [], offset  # no complete line yet
    complete = data[: last_nl + 1]
    lines = complete.decode("utf-8", errors="replace").splitlines()
    return lines, offset + len(complete)


def sig_hash(signature: str) -> str:
    return hashlib.sha256(signature.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------------- #
# Worker loop
# --------------------------------------------------------------------------------- #
def run_worker(args: argparse.Namespace) -> int:
    configure_worker_account()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    ckpt = Checkpoint(args.out_dir / "worker_checkpoint.json")
    extractions_path = args.out_dir / "extractions.jsonl"
    near_miss_path = args.out_dir / "near_miss.jsonl"
    trajectories_path = args.out_dir / "trajectories.jsonl"

    all_records: list[dict[str, Any]] = []
    extractions_by_sig: dict[str, BlockExtraction] = {}
    if extractions_path.exists():
        for line in extractions_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            signature = d.get("signature")
            if not signature:
                continue
            ext = BlockExtraction(**{k: v for k, v in d.items() if k != "signature"})
            extractions_by_sig[signature] = ext
        print(f"[worker] resumed {len(extractions_by_sig)} previously-completed extraction(s)", file=sys.stderr)

    stop_requested = {"flag": False}

    def _handle_stop(signum: int, frame: Any) -> None:
        print(f"\n[worker] received signal {signum} — finishing in-flight work, then stopping", file=sys.stderr)
        stop_requested["flag"] = True

    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    completed_since_rebuild = 0
    extractions_fh = extractions_path.open("a", encoding="utf-8")
    near_miss_fh = near_miss_path.open("a", encoding="utf-8")

    def rebuild_trajectories() -> None:
        chains = chain_records(all_records)
        extractions_by_key = index_extractions_by_signature(chains, extractions_by_sig)
        trajectories = build_trajectories(chains, extractions_by_key)
        tmp = trajectories_path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for traj in trajectories:
                fh.write(json.dumps(traj, ensure_ascii=False) + "\n")
        tmp.replace(trajectories_path)
        print(f"[worker] rebuilt {trajectories_path} ({len(trajectories)} chains, {len(all_records)} records seen)", file=sys.stderr)

    def process_one(record: dict[str, Any], block_index: int, do_sample: bool) -> BlockExtraction:
        return extract_block(
            chain_id=-1, turn_index=-1, block_index=block_index, record=record,
            max_tokens=args.max_tokens, attempts=args.attempts, verify=args.verify,
            accept_within=0, large_threshold=args.large_threshold_bytes,
            large_max_attempts=args.large_max_attempts, near_miss_bytes=args.near_miss_bytes,
            do_strict_sample=do_sample,
        )

    in_flight_hashes: set[str] = set()  # queued this run, not yet committed to ckpt — see above

    try:
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            while True:
                new_jobs: list[tuple[dict[str, Any], int, str]] = []
                for watch_dir in (Path(d) for d in args.watch_dir):
                    if not watch_dir.exists():
                        continue
                    for path in sorted(watch_dir.glob("*.jsonl")):
                        key = str(path.resolve())
                        offset = ckpt.file_offsets.get(key, 0)
                        lines, new_offset = read_new_complete_lines(path, offset)
                        if new_offset != offset:
                            ckpt.file_offsets[key] = new_offset
                        for line in lines:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                record = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            all_records.append(record)
                            for block_index in thinking_block_indices(record):
                                signature = record["response"]["content"][block_index].get("signature")
                                h = sig_hash(signature)
                                # Only dedupe against hashes already committed to disk, plus ones
                                # queued earlier in THIS run (in_flight_hashes below) — NOT marked
                                # into ckpt.seen_signature_hashes yet. That commit happens in
                                # record_result(), conditioned on the outcome, specifically so a
                                # total infra failure (e.g. hitting the gateway's requests/minute
                                # limit, cost_usd == 0, no real attempt ever completed) is retried
                                # on a later poll instead of being silently written off forever.
                                # Found the hard way: 55/106 blocks in one real run — including
                                # several large, high-value ones — got rate-limited on every
                                # attempt and would have been unrecoverable under the old
                                # "mark seen at discovery" behavior.
                                if h in ckpt.seen_signature_hashes or h in in_flight_hashes:
                                    continue
                                in_flight_hashes.add(h)
                                new_jobs.append((record, block_index, signature))

                if new_jobs:
                    print(f"[worker] {len(new_jobs)} new thinking block(s) queued for extraction", file=sys.stderr)

                    def record_result(ext: BlockExtraction, signature: str) -> None:
                        nonlocal completed_since_rebuild
                        # A total infra failure — no attempt ever got a response, so cost_usd is
                        # still 0 and verification never moved off "none" — is NOT committed to
                        # the persistent seen-set. It stays only in this run's in_flight_hashes,
                        # so a fresh process (or the same one, next poll, once the file offset
                        # already covers it) will pick it up and retry rather than silently
                        # losing it. Any real attempt (even a paid one that ended up unverified,
                        # or the free-summary path) IS committed — retrying those wouldn't
                        # obviously help and would just spend money again.
                        retryable_failure = ext.verification == "none" and ext.cost_usd == 0.0
                        if not retryable_failure:
                            ckpt.seen_signature_hashes.add(sig_hash(signature))
                        extractions_by_sig[signature] = ext
                        record_dict = asdict(ext)
                        record_dict["signature"] = signature
                        extractions_fh.write(json.dumps(record_dict, ensure_ascii=False) + "\n")
                        extractions_fh.flush()
                        if ext.near_miss:
                            near_miss_fh.write(json.dumps(record_dict, ensure_ascii=False) + "\n")
                            near_miss_fh.flush()
                        completed_since_rebuild += 1
                        print(
                            f"[worker]   verified={ext.verified} ({ext.verification}) sealed={ext.sealed_bytes} "
                            f"recovered={ext.recovered_bytes} large={ext.is_large} near_miss={ext.near_miss} "
                            f"${ext.cost_usd:.4f}" + ("  [retryable failure — not committed]" if retryable_failure else ""),
                            file=sys.stderr,
                        )

                    futures = {}
                    for record, block_index, signature in new_jobs:
                        target = sealed_length(signature)
                        is_large = args.large_threshold_bytes > 0 and target is not None and target >= args.large_threshold_bytes
                        use_free_summary = (
                            not is_large
                            and args.free_summary_threshold_bytes > 0
                            and target is not None
                            and target < args.free_summary_threshold_bytes
                            and bool(free_summary_text(record, block_index))
                        )
                        if use_free_summary:
                            # Zero-cost, no thread-pool slot spent — see
                            # extract_pipeline_capture.free_summary_extraction.
                            ext = free_summary_extraction(
                                chain_id=-1, turn_index=-1, block_index=block_index, record=record, target=target,
                            )
                            record_result(ext, signature)
                            continue
                        do_sample = is_large and ckpt.sample_used < args.strict_sample_count
                        if do_sample:
                            ckpt.sample_used += 1
                        fut = pool.submit(process_one, record, block_index, do_sample)
                        futures[fut] = signature

                    for fut in as_completed(futures):
                        signature = futures[fut]
                        try:
                            ext = fut.result()
                        except Exception as e:  # noqa: BLE001 — one bad job must not kill the worker
                            print(f"[worker] extraction crashed: {e}", file=sys.stderr)
                            continue
                        record_result(ext, signature)

                    ckpt.save()
                    if completed_since_rebuild >= args.rebuild_every:
                        rebuild_trajectories()
                        completed_since_rebuild = 0

                if stop_requested["flag"]:
                    break
                if args.once and not new_jobs:
                    break
                time.sleep(args.poll_interval)
    finally:
        ckpt.save()
        rebuild_trajectories()
        extractions_fh.close()
        near_miss_fh.close()
        near_miss_count = sum(1 for e in extractions_by_sig.values() if e.near_miss)
        free_count = sum(1 for e in extractions_by_sig.values() if e.verification == "summary")
        total_cost = sum(e.cost_usd for e in extractions_by_sig.values())
        print(
            f"[worker] stopped. {len(extractions_by_sig)} signature(s) processed total "
            f"({free_count} free-summary, $0), {near_miss_count} flagged near-miss, "
            f"${total_cost:.2f} spent.",
            file=sys.stderr,
        )

    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--watch-dir", action="append", required=True, help="capture dir(s) to watch; repeatable")
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    ap.add_argument("--poll-interval", type=float, default=DEFAULT_POLL_INTERVAL)
    ap.add_argument("--verify", choices=["strict", "length", "approximate"], default="strict")
    ap.add_argument("--attempts", type=int, default=DEFAULT_ATTEMPTS)
    ap.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    ap.add_argument("--large-threshold-bytes", type=int, default=DEFAULT_LARGE_THRESHOLD_BYTES)
    ap.add_argument("--large-max-attempts", type=int, default=DEFAULT_LARGE_MAX_ATTEMPTS)
    ap.add_argument("--strict-sample-count", type=int, default=DEFAULT_STRICT_SAMPLE_COUNT)
    ap.add_argument("--near-miss-bytes", type=int, default=DEFAULT_NEAR_MISS_BYTES)
    ap.add_argument(
        "--free-summary-threshold-bytes", type=int, default=DEFAULT_FREE_SUMMARY_THRESHOLD_BYTES,
        metavar="N", help="skip paid replay for blocks below this size that already have free "
        "summarized thinking text; 0 disables (always pay for verbatim extraction)",
    )
    ap.add_argument("--rebuild-every", type=int, default=DEFAULT_REBUILD_EVERY)
    ap.add_argument("--once", action="store_true", help="drain whatever is currently available, then exit")
    args = ap.parse_args(argv)
    return run_worker(args)


if __name__ == "__main__":
    raise SystemExit(main())
