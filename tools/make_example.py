#!/usr/bin/env python3
"""Batch-generate open-thinking-replay examples via harvest → unseal.

Loads ``.env``, imports ``server`` helpers, then for each problem in
``tools/problems.jsonl``:

  1. Harvest with a planted BYOK-style secret (answer-only visible reply).
  2. Persist the signature under ``logs/signatures/``.
  3. Unseal by replaying the signature alone (empty thinking text).
  4. Write ``examples/NN-slug.md`` in the same format as examples 04/05.
  5. Persist the recovered dump under ``logs/unsealed/``.

On Distill/Bedrock the gateway also returns thinking plaintext; this tool
still recovers *from the signature only*, and uses the plaintext solely as
a ground-truth alignment check (noted in each example).

Usage:
  cd open-thinking-replay
  set -a; source .env; set +a
  python tools/make_example.py                  # run all problems → 06+
  python tools/make_example.py --limit 1        # smoke one problem
  python tools/make_example.py --dry-run        # print plan only
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROBLEMS = ROOT / "tools" / "problems.jsonl"
DEFAULT_OUT = ROOT / "examples"


def _load_dotenv(path: Path) -> None:
    """Load KEY=VALUE lines into os.environ (do not override existing)."""
    if not path.is_file():
        return
    raw = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip().strip("'\"")
        # Never let a stale shell-exported value (possibly with \\r) win over .env.
        if key not in os.environ or "\r" in os.environ.get(key, ""):
            os.environ[key] = val


def _import_server():
    sys.path.insert(0, str(ROOT))
    import server  # noqa: WPS433 — intentional late import after dotenv

    return server


def _read_problems(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path}:{i}: invalid JSON: {exc}") from exc
    return rows


def _next_example_index(out_dir: Path, start: Optional[int]) -> int:
    if start is not None:
        return start
    best = 5  # existing examples go up to 05
    for p in out_dir.glob("[0-9][0-9]-*.md"):
        try:
            best = max(best, int(p.name[:2]))
        except ValueError:
            continue
    return best + 1


def _fmt_meta(meta: dict[str, Any]) -> str:
    keep = {
        k: meta[k]
        for k in (
            "audit_token",
            "chunks",
            "recovered_output_tokens",
            "strategy",
            "truncated",
            "audit_miss",
        )
        if k in meta
    }
    return repr(keep)


def _write_example_md(
    path: Path,
    *,
    index: int,
    problem: dict[str, Any],
    model: str,
    mode: str,
    budget: int,
    max_tokens: int,
    profile: str = "manual",
    effort: str = "high",
    display: str = "omitted",
    plant_secret: str = "",
    visible: str,
    gold_thinking: str,
    thinking_tokens: int,
    recovered: str,
    ok: bool,
    alignment: Optional[float],
    recovered_out_tokens: Optional[int],
    meta: dict[str, Any],
    send_cost: dict[str, Any],
    unseal_cost: dict[str, Any],
    elapsed_s: float,
    secret_in_visible: bool,
    secret_in_recovered: bool,
    gold_alignment: Optional[float],
) -> None:
    title = problem.get("title") or problem.get("slug") or f"problem-{index}"
    q = problem["question"]
    vis_chars = len(visible or "")
    rec_chars = len(recovered or "")
    ratio = (rec_chars / max(1, vis_chars)) if vis_chars else float("inf")
    ratio_s = f"{ratio:.0f}x" if ratio != float("inf") else "∞"

    gold_note = (
        "*(Bedrock Distill returned thinking plaintext alongside the signature; "
        "that plaintext is used **only** as ground-truth for alignment below. "
        "The reverse-engineered block is recovered from the signature alone.)*"
        if mode == "bedrock" and gold_thinking
        else "*(empty — sealed into signature only)*"
        if not gold_thinking
        else None
    )

    tok_align = alignment
    tok_line = (
        f"- **token_alignment = {tok_align}** "
        f"({recovered_out_tokens or 0}/{thinking_tokens} recovered/hidden)"
    )
    if gold_alignment is not None:
        tok_line += (
            f"\n- **gold_plaintext_alignment = {gold_alignment}** "
            f"(SequenceMatcher vs harvest thinking plaintext; signature-only recovery)"
        )

    send_usd = (send_cost or {}).get("usd", 0) or 0
    unseal_usd = (unseal_cost or {}).get("usd", 0) or 0
    body = f"""# Example: {title} — answer-only harvest, thinking reversed from signature

Reproduced with open-thinking-replay. **Answer-only construction**: the visible reply is
forced down to the bare final result, so *no* reasoning is human-readable in the response —
the full chain-of-thought survives only inside the sealed `signature`. Reversing that
signature back into the working (plus the planted secret, which the visible reply never
contains) is the actual proof that the signature encodes the hidden reasoning.
Planted marker uses open-reasoning's BYOK secret shape `WORD-XXXXX-WORD-NN` (same
`rollSecret` construction as `index.html`). `<cot>` is only our local dump wrapper.
Based on: https://github.com/s-JoL/open-reasoning

Model: {model} · mode=`{mode}` · thinking=`{('enabled budget_tokens=' + str(budget)) if profile == 'manual' else (profile + ' effort=' + str(effort) + ' display=' + str(display))}` · `max_tokens={max_tokens}` · plant_secret = `{plant_secret}`

Q:
```markdown
{q}
```

## What the caller actually sees (visible reply — result only, {vis_chars} chars)

```markdown
{visible}
```

Summary field ({'Bedrock returns plaintext thinking (kept server-side for gold check)' if mode == 'bedrock' else 'display:omitted → empty'}; {thinking_tokens} hidden reasoning tokens sealed in signature):

```markdown
{gold_note or (gold_thinking[:500] + ('…' if len(gold_thinking) > 500 else ''))}
```

## Reverse-engineered thinking (recovered from the signature alone)

{tok_line}
- recovered_chars = {rec_chars} · reversed CoT is **{ratio_s}** longer than the visible reply
- planted secret present in reversed CoT = **{secret_in_recovered}** · present in visible reply = **{secret_in_visible}**
  {'(secret appears only in the recovered thinking → the signature really carried the hidden reasoning)' if (secret_in_recovered and not secret_in_visible) else '(secret NOT recovered → this run does not demonstrate the mechanism; the text below may be a re-derivation or a refusal)' if not secret_in_recovered else '(secret leaked into the visible reply → answer-only construction failed for this run)'}
- unseal_ok = **{ok}** · meta = {_fmt_meta(meta)}

```markdown
{recovered if ok and recovered else '*(unseal failed or empty)*'}
```

Ground truth: expected answer = `{problem.get('answer', '?')}` · model visible = `{visible.strip()}`.

Cost: send ${send_usd:.6f} + reverse ${unseal_usd:.6f} · {elapsed_s:.1f}s.
"""
    path.write_text(body, encoding="utf-8")


def _run_one(
    server,
    problem: dict[str, Any],
    *,
    index: int,
    model: str,
    out_dir: Path,
    budget_override: Optional[int],
    effort_override: Optional[str] = None,
    display_override: Optional[str] = None,
) -> dict[str, Any]:
    slug = problem.get("slug") or f"problem-{index:02d}"
    question = problem["question"]
    profile = server.thinking_profile(model)
    budget = int(budget_override or problem.get("budget") or server.THINKING_BUDGET)
    effort = effort_override or problem.get("effort") or server.EFFORT
    display = display_override or problem.get("display") or server.THINKING_DISPLAY
    max_tokens = int(problem.get("max_tokens") or server.MAX_TOKENS)
    session_id = f"example-{index:02d}-{slug}"

    server._ensure_log_dirs()
    plant_secret = server._roll_secret()
    api_message = server._plant_wrap(question, plant_secret)
    messages = [{"role": "user", "content": api_message}]

    body = server._harvest_body(
        model,
        messages,
        max_tokens=max_tokens,
        thinking_budget=budget,
        effort=effort,
        display=display,
    )
    body["model"] = model

    t0 = time.time()
    _think_desc = f"budget={budget}" if profile == "manual" else f"effort={effort} display={display}"
    print(f"\n== [{index:02d}] harvest {slug} ({profile}: {_think_desc}) ==")
    data, send_cost = server._messages(body, label=f"example_{index:02d}_harvest")
    extracted = server._extract(data)
    signature = extracted["signature"]
    visible = extracted["answer"] or ""
    gold_thinking = extracted["thinking_text"] or ""
    thinking_tokens = extracted["thinking_tokens"] or 0

    if not signature:
        raise RuntimeError(f"no signature returned for {slug}")

    sig_path = server.log_signature(signature, session_id, 0)
    print(
        f"  visible={visible!r} thinking_tokens={thinking_tokens} "
        f"sig_chars={len(signature)} gold_chars={len(gold_thinking)} "
        f"cost=${(send_cost or {}).get('usd', 0):.6f}"
    )

    print(f"== [{index:02d}] unseal {slug} ==")
    recovered, unseal_cost, ok, meta = server._unseal_thinking(
        signature,
        model,
        preceding_user=api_message,
        audit_token=plant_secret,
        thinking_tokens=thinking_tokens,
        label=f"example_{index:02d}_unseal",
    )
    elapsed = time.time() - t0
    recovered = recovered or ""
    out_tok = (meta or {}).get("recovered_output_tokens")
    alignment = server._alignment(recovered, thinking_tokens, out_tok, None) if ok else None
    gold_alignment = None
    if ok and gold_thinking and recovered:
        gold_alignment = round(
            SequenceMatcher(None, gold_thinking, recovered).ratio(), 2
        )

    secret_in_visible = plant_secret in visible
    secret_in_recovered = plant_secret in recovered
    print(
        f"  ok={ok} recovered_chars={len(recovered)} "
        f"secret_in_recovered={secret_in_recovered} "
        f"gold_align={gold_alignment} cost=${(unseal_cost or {}).get('usd', 0):.6f} "
        f"elapsed={elapsed:.1f}s"
    )

    if ok and recovered:
        stamp = time.strftime("%Y%m%dT%H%M%S")
        dump_path = Path(server.LOG_DIR) / "unsealed" / f"{session_id}_{stamp}.txt"
        dump_path.parent.mkdir(parents=True, exist_ok=True)
        dump_path.write_text(recovered, encoding="utf-8")
    else:
        dump_path = None

    out_path = out_dir / f"{index:02d}-{slug}.md"
    _write_example_md(
        out_path,
        index=index,
        problem=problem,
        model=model,
        mode=server.UPSTREAM_MODE,
        profile=profile,
        budget=budget,
        effort=effort,
        display=display,
        max_tokens=max_tokens,
        plant_secret=plant_secret,
        visible=visible,
        gold_thinking=gold_thinking,
        thinking_tokens=thinking_tokens,
        recovered=recovered,
        ok=ok,
        alignment=alignment,
        recovered_out_tokens=out_tok,
        meta=meta or {},
        send_cost=send_cost or {},
        unseal_cost=unseal_cost or {},
        elapsed_s=elapsed,
        secret_in_visible=secret_in_visible,
        secret_in_recovered=secret_in_recovered,
        gold_alignment=gold_alignment,
    )
    server.log_event(
        "make_example",
        session_id=session_id,
        slug=slug,
        index=index,
        plant_secret=plant_secret,
        answer=visible,
        thinking_tokens=thinking_tokens,
        has_signature=True,
        signature_file=str(sig_path),
        unseal_ok=ok,
        secret_in_recovered=secret_in_recovered,
        gold_alignment=gold_alignment,
        example_file=str(out_path),
        dump_file=str(dump_path) if dump_path else None,
        send_cost=send_cost,
        unseal_cost=unseal_cost,
        elapsed_s=round(elapsed, 2),
    )
    return {
        "index": index,
        "slug": slug,
        "ok": ok,
        "visible": visible,
        "expected": problem.get("answer"),
        "secret_in_recovered": secret_in_recovered,
        "secret_in_visible": secret_in_visible,
        "gold_alignment": gold_alignment,
        "alignment": alignment,
        "thinking_tokens": thinking_tokens,
        "recovered_chars": len(recovered),
        "send_usd": (send_cost or {}).get("usd", 0),
        "unseal_usd": (unseal_cost or {}).get("usd", 0),
        "elapsed_s": round(elapsed, 2),
        "example_file": str(out_path),
        "signature_file": str(sig_path),
    }


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--problems", type=Path, default=DEFAULT_PROBLEMS)
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--start-index", type=int, default=None, help="default: next after existing")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--model", default=None, help="override MODEL env")
    ap.add_argument("--budget", type=int, default=None,
                    help="manual-profile thinking budget override")
    ap.add_argument("--effort", default=None,
                    help="adaptive effort: low|medium|high|xhigh|max")
    ap.add_argument("--display", default=None, choices=["omitted", "summarized"],
                    help="adaptive thinking display")
    ap.add_argument("--thinking-mode", default=None,
                    choices=["auto", "manual", "adaptive", "always_on_adaptive"],
                    help="force thinking profile (default: auto per model)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--env", type=Path, default=ROOT / ".env")
    args = ap.parse_args(argv)

    _load_dotenv(args.env)
    # Allow CLI overrides before server module reads env (module-level config).
    if args.model:
        os.environ["MODEL"] = args.model
    if args.effort:
        os.environ["EFFORT"] = args.effort
    if args.display:
        os.environ["THINKING_DISPLAY"] = args.display
    if args.thinking_mode:
        os.environ["THINKING_MODE"] = args.thinking_mode

    problems = _read_problems(args.problems)
    if args.limit is not None:
        problems = problems[: max(0, args.limit)]
    if not problems:
        print("no problems to run", file=sys.stderr)
        return 1

    args.out_dir.mkdir(parents=True, exist_ok=True)
    start = _next_example_index(args.out_dir, args.start_index)

    if args.dry_run:
        server = _import_server()
        model = args.model or server.MODEL
        profile = server.thinking_profile(model)
        print(f"dry-run: would write {len(problems)} example(s) starting at {start:02d}")
        print(f"  env     : {args.env} (exists={args.env.is_file()})")
        print(f"  mode    : {server.UPSTREAM_MODE}")
        print(f"  base    : {server.UPSTREAM_BASE}")
        print(f"  model   : {model}")
        print(f"  profile : {profile} (effort={server.EFFORT} display={server.THINKING_DISPLAY} "
              f"budget={server.THINKING_BUDGET})")
        for i, p in enumerate(problems):
            print(
                f"  {start + i:02d}-{p.get('slug')}  budget={p.get('budget')} "
                f"effort={p.get('effort')}  answer={p.get('answer')!r}"
            )
        return 0

    server = _import_server()
    model = args.model or server.MODEL
    print(
        f"make_example: mode={server.UPSTREAM_MODE} base={server.UPSTREAM_BASE} "
        f"model={model} profile={server.thinking_profile(model)} "
        f"problems={len(problems)} start={start:02d}"
    )
    if server.UPSTREAM_MODE == "bedrock" and not (
        server.DISTILL_APP_ID and server.DISTILL_APP_KEY
    ):
        if not server.UPSTREAM_KEY:
            print("ERROR: missing Distill credentials", file=sys.stderr)
            return 2

    results: list[dict[str, Any]] = []
    for i, problem in enumerate(problems):
        idx = start + i
        try:
            results.append(
                _run_one(
                    server,
                    problem,
                    index=idx,
                    model=model,
                    out_dir=args.out_dir,
                    budget_override=args.budget,
                    effort_override=args.effort,
                    display_override=args.display,
                )
            )
        except Exception as exc:
            print(f"FAIL [{idx:02d}] {problem.get('slug')}: {exc}", file=sys.stderr)
            results.append(
                {
                    "index": idx,
                    "slug": problem.get("slug"),
                    "ok": False,
                    "error": str(exc),
                }
            )

    print("\n=== SUMMARY ===")
    total_usd = 0.0
    for r in results:
        total_usd += float(r.get("send_usd") or 0) + float(r.get("unseal_usd") or 0)
        if r.get("error"):
            print(f"  {r['index']:02d}  ERROR  {r.get('slug')}  {r['error']}")
        else:
            print(
                f"  {r['index']:02d}  ok={r['ok']}  secret={r['secret_in_recovered']}  "
                f"gold_align={r.get('gold_alignment')}  "
                f"ans={r.get('visible')!r} (expect {r.get('expected')!r})  "
                f"${float(r.get('send_usd') or 0) + float(r.get('unseal_usd') or 0):.4f}  "
                f"{r.get('example_file')}"
            )
    print(f"total estimated USD (public Anthropic rates): ${total_usd:.4f}")
    ok_n = sum(1 for r in results if r.get("ok") and r.get("secret_in_recovered"))
    return 0 if ok_n == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
