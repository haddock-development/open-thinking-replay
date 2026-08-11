# Open Thinking Replay

Recover Claude's raw private chain-of-thought from a thinking `signature` — and **prove
locally that what you got is the sealed original**, not a re-derivation and not a summary.

从 Claude 的 thinking `signature` 中还原模型的原始私有推理，并且能**在本地证明拿到的就是被
封存的原文**，而不是模型重新推导的内容，也不是摘要。

```bash
python tools/extract_cot.py --model claude-opus-5 --effort max \
  --question "Find the units digit of 7^2024."
```

```
verified=True (strict) sealed=123 recovered=123 attempts=3
  note: attempt 1: recovered 960 B vs sealed 123 B (off by 837)   <- rejected
  note: attempt 2: length verified, awaiting a matching repeat
```

That first attempt is the whole point. It was a fluent, plausible reasoning trace — and it
was **wrong**, a rewrite rather than the sealed original. A pipeline without the verifier
would have kept it.

第一次尝试正是关键所在：它是一段流畅可信的推理，但**并不是**密封原文，而是改写。没有验证器的
流程会把它当作结果保留下来。

> Based on the original **Open Reasoning** project by s-JoL
> ([github.com/s-JoL/open-reasoning](https://github.com/s-JoL/open-reasoning), now returning
> 404). That project published the demo and unlocked examples but not the recovery
> implementation; this repository reproduces the server side and adds the local verifier,
> the multi-provider gateway adapter, and multi-model measurements. `static/index.html` is a
> vendored copy of its demo frontend. See [LICENSE](LICENSE) for provenance and licensing.
>
> 本仓库基于 s-JoL 的原始 **Open Reasoning** 项目
> （[github.com/s-JoL/open-reasoning](https://github.com/s-JoL/open-reasoning)，现返回 404）。
> 该项目公开了演示与解锁样例，但未公开还原实现；本仓库复现了服务端，并新增本地验证器、
> 多 provider 网关适配与多模型实测记录。`static/index.html` 是其演示前端的 vendored 副本。
> 来源与授权说明见 [LICENSE](LICENSE)。



## How you know it's the real thinking / 如何确认拿到的是真实推理

The signature is a protobuf envelope whose inner field `#5` is an **AES-GCM ciphertext**.
GCM appends a 16-byte authentication tag and does not pad, so

signature 是一个 protobuf 信封，内层字段 `#5` 是 **AES-GCM 密文**。GCM 追加 16 字节认证标签
且不做填充，因此：

```
len(sealed_plaintext_utf8) == ciphertext_len − 16
```

`ciphertext_len` is readable **locally, from the signature alone**. You therefore know the
exact byte count of the hidden reasoning *before the model says anything*. Two independent
checks follow:

`ciphertext_len` 可**仅凭 signature 在本地读出**，因此在模型开口之前就已知隐藏推理的精确字节数。
由此得到两项独立检查：


| Check                                                                  | What it rules out                                                          | 排除的可能                  |
| ---------------------------------------------------------------------- | -------------------------------------------------------------------------- | ---------------------- |
| **length** — recovered UTF-8 bytes equal the sealed byte count exactly | a summary (far too short) and a re-derivation (lands elsewhere)            | 摘要（远短于原文）与重新推导（长度落在别处） |
| **determinism** — two independent replays return byte-identical text   | a trace the model invents fresh each time, which would vary under sampling | 每次现编的轨迹（采样下必然变化）       |


`--verify strict` (default) requires both. Neither check asks the provider to confirm
anything, so the guarantee does not depend on trusting the gateway.

`--verify strict`（默认）要求两项同时满足。两项检查都不需要 provider 确认，因此该保证不依赖于
信任网关。

**Honest limits.** On short traces a failed attempt misses by hundreds of bytes, so an exact
length match is already decisive. On multi-KB traces the attempts cluster tightly around the
true length — misses of 14 and 23 bytes on a 22,627-byte trace were observed — so there an
exact match is strong but not decisive alone, and only `strict` removes the residual risk of
a near-perfect transcript whose small errors cancel. `--accept-within N` adds an explicitly
weaker `approximate` tier for bulk collection.

**诚实的边界**：短轨迹上失败的尝试会偏离数百字节，长度精确命中即可定论；多 KB 轨迹上各次尝试
紧密聚集在真值附近（22,627 字节的轨迹上观测到偏差 14 与 23 字节），此时精确命中虽强但不足以单独
定论，只有 `strict` 能排除「近乎完美但误差相互抵消」的残余风险。`--accept-within N` 提供一个
明确标注为更弱的 `approximate` 档，供批量采集使用。

## Which model to use / 模型选择

**Use Claude Opus 5.** Across 15 strict-protocol cases (short arithmetic on two routes plus
AIME 2025), it is the only model that both answered everything correctly and recovered the
sealed trace byte-exactly most of the time:

**推荐 Claude Opus 5。** 在 15 例严格协议测试中（两条路由的短算术题加 AIME 2025），它是唯一
既全部答对、又在多数情况下逐字节还原密封轨迹的模型：


| Model             | Cases | Answers correct | Byte-exact recovery | AIME cost | Mean thinking tokens |
| ----------------- | ----- | --------------- | ------------------- | --------- | -------------------- |
| `claude-opus-5`   | 15    | **15/15**       | **10/15**           | $6.90     | 6857                 |
| `claude-opus-4-8` | 15    | 14/15           | 2/15                | $5.63     | 9084                 |
| `claude-opus-4-7` | 5     | 4/5             | 2/5                 | $10.19    | 12981                |
| `claude-opus-4-6` | 5     | 4/5             | 1/5                 | $12.32    | 13192                |
| `claude-fable-5`  | 5     | —               | 0/5                 | —         | —                    |


- **Opus 4.8** answers well but usually declines to transcribe, treating the request as
copying an injected token; it rewrites instead. It also leaked a planted marker into its
own visible reply once.
  
Opus 4.8 作答良好，但通常拒绝逐字转录，改为重写；还曾把植入标记写进可见回答。
- **Fable 5** is blocked by a dedicated server-side classifier: a verbatim-transcription
request returns `{"type":"refusal","category":"reasoning_extraction"}` with an empty
`content` array. Do not use it for this.
  
Fable 5 被专门的服务端分类器拦截，逐字转录请求返回
`{"type":"refusal","category":"reasoning_extraction"}` 且 `content` 为空数组，不适用。
- **Raise** `max_tokens`**.** At 32000 the *harvest itself* truncates on hard problems (three
models hit the cap on AIME I P15 and answered wrong). The ceiling is 128000.
  
`max_tokens` 要调高：32000 时难题的 harvest 会被截断（三个模型在 AIME I P15 触顶并答错），
上限为 128000。

Full per-model measurements: **[docs/reproduction-log.md](docs/reproduction-log.md)**.

## Related work / 相关工作

This repo's original scope is narrower and more conservative than the broader literature on
this class of vulnerability: **same-model** replay only, accepted exclusively on the two
locally-computable checks in the previous section (never on the provider's own claims), and
with no requirement to plant a secret to prove recovery (§"Verified extraction without a
planted secret", [docs/reproduction-log.md](docs/reproduction-log.md) §4f). Two independent
lines of published work establish the wider architectural picture this repo's method sits
inside:

- Matthew Green, ["Fooling around with encrypted reasoning
  blobs"](https://blog.cryptographyengineering.com/2026/05/29/fooling-around-with-encrypted-reasoning-blobs/)
  (2026-05-29) — the original disclosure that Anthropic's and OpenAI's encrypted reasoning
  envelopes are authenticated under what appears to be a single global key, making them
  replayable out of order, across sessions, and across accounts, with no per-session or
  per-user binding.
- Panfilov, Schmotz, Shumailov, Beurer-Kellner, Schaeffer, Prabhu, Geiping & Andriushchenko,
  ["Stealing Reasoning Traces from Proprietary LLM
  APIs"](https://arxiv.org/abs/2608.09867) (arXiv:2608.09867, 2026) — building on Green's
  finding, shows the same envelopes are also portable **across different models** within a
  provider's family, and uses this to inject a stronger model's signature into a weaker,
  less-safeguarded sibling (e.g. Opus 4.8 → Haiku 4.5) that can be induced to transcribe it
  verbatim without the stronger model ever being directly asked to reveal anything. They
  also report large-scale PII/credential recovery from public agent-trace datasets and an
  invisible-prompt-injection vector via poisoned reasoning blocks.

We independently reproduced the second paper's Claude cross-model method against our own
account and extended it with a check the paper does not perform — whether it transfers to a
model released after the paper's stated test window. It reproduces cleanly on
`claude-opus-4-8` (byte-exact against a same-model ground truth) but does **not** currently
transfer to `claude-opus-5` (6/6 real signatures failed under two injection styles). See
[`tools/cross_model_decode.py`](tools/cross_model_decode.py) for a runnable implementation
and [docs/reproduction-log.md](docs/reproduction-log.md), §8.6, for the full writeup.

本仓库的原始范围比这一类漏洞的广义文献更窄、更保守：只做**同模型**回放，只依据上一节两项
可在本地计算的检查项来判定（从不采信 provider 自身的说法），且证明还原不需要植入 secret
（见"无植入 secret 的可验证提取"，[docs/reproduction-log.md](docs/reproduction-log.md) §4f）。
以下两项独立发表的工作构成了本仓库方法所处的更大架构图景：

- Matthew Green，["Fooling around with encrypted reasoning
  blobs"](https://blog.cryptographyengineering.com/2026/05/29/fooling-around-with-encrypted-reasoning-blobs/)
  （2026-05-29）——最早披露 Anthropic 与 OpenAI 的加密推理信封似乎由单一全局密钥认证，
  因此可以乱序重放、跨会话重放、甚至跨账号重放，没有任何按会话或按用户的绑定。
- Panfilov 等，["Stealing Reasoning Traces from Proprietary LLM
  APIs"](https://arxiv.org/abs/2608.09867)（arXiv:2608.09867，2026）——在 Green 的发现基础上，
  证明同样的信封在同一供应商的模型家族内**跨模型**也可移植，并以此把强模型的 signature
  注入到防护更弱的家族内兄弟模型（如 Opus 4.8 → Haiku 4.5），诱导其逐字转录，全程无需直接
  要求强模型透露任何内容。论文还报告了从公开 agent 轨迹数据集中大规模恢复 PII/凭证，以及
  通过被投毒的推理块实现隐形提示注入的攻击面。

我们在自己的账号上独立复现了该论文针对 Claude 的跨模型方法，并补充了论文未做的一项检验——
该方法是否适用于晚于论文测试窗口发布的模型。结果：在 `claude-opus-4-8` 上干净复现（与同模型
ground truth 逐字节一致），但**未能**扩展到 `claude-opus-5`（两种注入方式下 6/6 条真实
signature 全部失败）。可运行实现见 [`tools/cross_model_decode.py`](tools/cross_model_decode.py)，
完整记录见 [docs/reproduction-log.md](docs/reproduction-log.md) §8.6。

## What's here / 仓库内容

- **[ANALYSIS.md](ANALYSIS.md)** — the `signature` wire format (base64 protobuf wrapping an
AEAD-encrypted copy of the private reasoning), entropy measurements, the length oracle, and
the adjudication of whether recovery is verbatim.

  
signature 的字段结构、熵值测量、长度预言机，以及「还原是否逐字」的判定过程。

- **[docs/reproduction-log.md](docs/reproduction-log.md)** — every experiment run, including
the failures: refusals, route outages, and the criterion changes that moved the numbers.

  
全部实验记录（含失败）：拒绝、路由不可用，以及判据变化对结果的影响。

- `tools/extract_cot.py` — verified extraction (the recommended entry point).

  
可验证提取工具，推荐的入口。

- `tools/decode_signature.py` — standalone protobuf decoder for any signature.

  
独立的 signature protobuf 解码器。

- `tools/make_example.py` — batch harvest → unseal with a planted secret, for reproducing the
mechanism proof rather than collecting traces.

  
带植入 secret 的批量 harvest → unseal，用于复现机制证明而非采集轨迹。

- `tools/extract_pipeline_capture.py` — batch extraction from an *already-running* live
agent harness's capture log (JSONL of `{route, model, request, response}`, one line per
model call — see e.g. a network tap sitting between an agent SDK's CLI subprocess and its
upstream), rather than from signatures this repo harvested itself. Regroups calls into
per-session chains by matching how one call's `messages` extends a prior call's
`messages + response` (no session id needed from the tap), replays every `thinking`
block's signature with a cheap, non-original prefix (replaying with the full original
multi-turn context did **not** help recovery in testing — see
`tools/test_extract_pipeline_capture.py` and the chaining/prefix-selection rationale in
its module docstring), and writes both a flat per-block extraction log and reconstructed
trajectories with each block's summarized/omitted text replaced by the verified recovery.

  
从**外部 agent harness 已产生**的调用日志（JSONL，一行一次模型调用的完整请求/响应）做批量抽取，
而非本仓库自己 harvest 的 signature。按“某次调用的 messages 是否恰好延伸自前一次调用的
messages+response”把日志重组为按会话的调用链（tap 不需要写入 session id），对每个 thinking
block 的 signature 用低成本、非原始上下文的方式回放（测试中，完整复现原始多轮上下文并未帮助
恢复——见模块文档字符串），输出打平的逐 block 抽取记录与重建后的完整轨迹。

- `tools/thinking_extract_worker.py` — a continuous, decoupled background worker built on
top of `extract_pipeline_capture.py`: polls one or more capture directories for newly
appended thinking blocks, extracts and verifies each with cost-tiered rules (cheap
`length`-only verification above a size threshold, an optional sampled `strict`
confirmation, a free zero-cost tier from harvest-time summaries, near-miss flagging), and
incrementally rebuilds a trajectories file — all without adding latency to whatever live
process is producing the capture log. See docs/reproduction-log.md §8.3–§8.5 for the cost
analysis behind the defaults.

  
基于 `extract_pipeline_capture.py` 的持续、解耦后台 worker：轮询一个或多个 capture 目录里新
追加的 thinking block，按成本分档规则抽取并验证（超过体量阈值只做 `length` 档、可选抽样
`strict` 复验、来自 harvest 时摘要的零成本档、near-miss 标记），并增量重建轨迹文件——全程不为
产生 capture 日志的实时进程增加任何延迟。分档规则背后的成本分析见 docs/reproduction-log.md
§8.3–§8.5。

- `tools/cross_model_decode.py` — reproduces
[Panfilov et al. (2026)](https://arxiv.org/abs/2608.09867)'s cross-model decoding method:
inject a signature produced by one model into a different, weaker sibling model from the
same provider family and have it transcribe the reasoning verbatim, verified against the
same length oracle used everywhere else in this repo. Confirmed working for
`claude-opus-4-8 → claude-haiku-4-5-20251001`; confirmed **not** to transfer to
`claude-opus-5` as of this writing — see the module docstring and
[docs/reproduction-log.md §8.6](docs/reproduction-log.md) before relying on any other pair.

  
复现 [Panfilov 等 (2026)](https://arxiv.org/abs/2608.09867) 的跨模型解码方法：把一个模型产生的
signature 注入同一供应商家族内另一个更弱的兄弟模型，诱导其逐字转录，用本仓库统一的长度预言机
验证。已确认 `claude-opus-4-8 → claude-haiku-4-5-20251001` 有效；已确认截至目前**不**适用于
`claude-opus-5`——在依赖其他模型组合前，请先阅读模块文档字符串与
[docs/reproduction-log.md §8.6](docs/reproduction-log.md)。

- `server.py` + `static/index.html` — the Flask backend and vendored demo frontend.

  
Flask 后端与内置演示前端。

## Worked examples / 实测样例

Each file is a complete run: the question, what the caller saw, and the recovered reasoning
with its verification evidence.

每个文件都是一次完整运行：题目、调用方看到的内容、还原出的推理及其验证证据。


| Example                                                                                                                                                | Model                | What it demonstrates                                                                                                                                                                                                 |
| ------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [13](examples/13-verified-aime-2025-i-p7.md)                                                                                                           | Opus 5               | **Verified extraction, no planted secret** — 3059-byte sealed trace recovered byte-exactly and confirmed by a byte-identical repeat. The trace contains false starts and self-corrections.                           |
| [09](examples/09-multiply-144-37.md) [10](examples/10-gcd-84-126.md) [11](examples/11-units-digit-3-pow-100.md) [12](examples/12-divisor-count-360.md) | Opus 5               | **Mechanism proof** — answer-only harvest with a planted marker; `ciphertext_len − recovered_bytes = 16` in every one, with a no-signature control confirming the marker came from the signature and not the prompt. |
| [04](examples/04-aime-walking-rates.md) [05](examples/05-cubic-squared-roots.md)                                                                       | Sonnet 4.6           | The original-author construction: `display=omitted`, empty summary, visible reply is the bare answer, proof rests on the planted marker.                                                                             |
| [06](examples/06-units-digit-seven.md) [07](examples/07-trailing-zeros-100.md) [08](examples/08-heron-divisor-sum.md)                                  | Sonnet 4.5 (Bedrock) | Early runs kept for honesty: the marker is recovered but the text is a paraphrase, which is what motivated the byte-exact criterion.                                                                                 |


Examples 04 and 05 follow the construction published by the original **Open Reasoning**
project; 06–08 are the same method on a Bedrock route and are deliberately kept even though
they fail the stricter criterion adopted later.

示例 04、05 沿用原 **Open Reasoning** 项目公开的构造；06–08 是同一方法在 Bedrock 路由上的运行，
虽未通过后来采用的更严格判据，仍保留以便对照。

## How it works / 工作原理

Two provider calls implement the whole demo (see ANALYSIS.md §4):

整个演示由两次对上游 provider 的调用构成（详见 ANALYSIS.md 第 4 节）：

1. **Harvest** — send a normal request with extended thinking. The provider returns a
  `thinking` block containing an opaque `signature` (the sealed reasoning) plus a visible
   answer. The plaintext reasoning never leaves the provider.
  **Harvest（采集）**：发送一次开启扩展思考（extended thinking）的普通请求。provider 会返回一个
   `thinking` 块，其中包含不透明的 `signature`（被封存的推理）以及可见答案。**明文推理永远不会离开 provider。**
2. **Replay (unseal)** — paste that `signature` back as an `assistant.thinking` block in a
  fresh request and ask the model to recite its prior private reasoning. The provider
   decrypts the sealed blob server-side and the model surfaces it.
  **Replay / Unseal（回放 / 解封）**：把这个 `signature` 作为 `assistant.thinking` 块重新塞进一个
   全新的请求里，再让模型复述它此前的私有推理。provider 会在服务端解密被封存的数据，模型随即将其还原出来。

The server reads the **bound model name** straight out of the signature header (via
`tools/decode_signature.py`) so replays always use the matching model and pass the AEAD auth
check.

服务端会直接从 signature 的头部读取**绑定的模型名**（通过 `tools/decode_signature.py`），
因此每次回放都会使用匹配的模型，从而通过 AEAD 的完整性校验。

### How the `signature` moves through the API / signature 如何在 API 中传递

The `signature` is never a top-level field of a request or response — it always rides **inside a**
`thinking` **content block**. It enters and leaves the server at three distinct points:

`signature` 从来不是请求或响应的顶层字段——它始终**藏在某个** `thinking` **内容块里**。它在三个不同的
环节进出服务端：

1. **Harvest — read out of the upstream response.** `POST /api/chat/send` calls the Messages API
  with `"thinking": {"type": "adaptive", "display": "omitted"}` (see `server.py`), which tells the
   provider to seal the CoT into the signature instead of returning plaintext. `_extract()` then
   walks `data["content"]` and pulls `block["signature"]` from the `thinking` (or
   `redacted_thinking`) block.
  **Harvest（采集）——从上游响应里读出。** `POST /api/chat/send` 调用 Messages API 时带上
   `"thinking": {"type": "adaptive", "display": "omitted"}`（见 `server.py`），告诉 provider 把 CoT
   封存进 signature 而不是返回明文。随后 `_extract()` 遍历 `data["content"]`，从 `thinking`
   （或 `redacted_thinking`）块中取出 `block["signature"]`。
2. **Server-side only — the raw signature is not sent to the browser.** In Live Conversation the
  harvested signature is stored in in-memory session state (`sess["turns"][i]["signature"]`) and
   written to `logs/signatures/`. The `/api/chat/send` JSON reply only exposes a boolean
   `has_signature`; the client later references a turn by index, never by the signature string.
  **仅服务端持有——原始 signature 不会发给浏览器。** 在 Live Conversation 中，采集到的 signature
   被存进内存会话状态（`sess["turns"][i]["signature"]`）并落盘到 `logs/signatures/`。
   `/api/chat/send` 的 JSON 响应只暴露一个布尔字段 `has_signature`；客户端之后是按轮次下标（turn
   index）引用某一轮，而不是传回 signature 字符串。
3. **Replay — written back into an** `assistant.thinking` **block.** `_replay_prompt()` rebuilds a
  messages array whose assistant turn contains `{"type": "thinking", "thinking": "", "signature":  sig}` followed by a `text` block, then a fresh user turn asking the model to recite its working.
   The signature reaches this function two ways:
  - `POST /api/byok/unseal` — the browser supplies it directly in the request body as
  `{"signature": "..."}` (BYOK / Prove It Yourself).
  - `POST /api/chat/unseal` — the browser sends only `{"session_id", "turn"}`; the server looks the
  signature up from its own session state and replays it.
  **Replay（回放）——写回一个** `assistant.thinking` **块。** `_replay_prompt()` 重建一个 messages
   数组，其中 assistant 轮包含 `{"type": "thinking", "thinking": "", "signature": sig}` 以及一个
   `text` 块，随后再接一个全新的 user 轮，要求模型复述其推理。signature 通过两条路径抵达该函数：
  - `POST /api/byok/unseal`——浏览器在请求体里直接以 `{"signature": "..."}` 传入（BYOK / Prove It Yourself）。
  - `POST /api/chat/unseal`——浏览器只发送 `{"session_id", "turn"}`；服务端从自己的会话状态里查出对应
  signature 并回放。



## Quick start / 快速开始

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env      # then edit UPSTREAM_KEY / MODEL as needed / 然后按需修改 UPSTREAM_KEY / MODEL
set -a; source .env; set +a
```

**Extract verified reasoning traces** — the main use case:

**提取经验证的推理轨迹**（主要用法）：

```bash
# one question, strict verification
python tools/extract_cot.py --model claude-opus-5 --effort max \
  --max-tokens 120000 --question "Find the units digit of 7^2024."

# batch: one JSON object with a "question" field per line
python tools/extract_cot.py --input problems.jsonl --out traces.jsonl \
  --model claude-opus-5 --effort max --max-tokens 120000 --attempts 4

# bulk collection, weaker assurance: keep traces within 32 bytes of the sealed length
python tools/extract_cot.py --input problems.jsonl --out traces.jsonl \
  --verify approximate --accept-within 32
```

Each output record carries `verified`, `verification` (`strict` / `length` / `approximate` /
`none`), `sealed_bytes`, `recovered_bytes` and `sha256`, so a downstream consumer can filter
by assurance level rather than trusting every row equally.

每条输出记录都带有 `verified`、`verification`（`strict` / `length` / `approximate` / `none`）、
`sealed_bytes`、`recovered_bytes` 与 `sha256`，下游可按保证等级筛选，而不必对每一行同等信任。

**Cross-model decoding** — cheaper, when your model pair is one of the confirmed-working
ones (see [Related work](#related-work--相关工作)):

**跨模型解码**——当你的模型组合已被确认可用时（见[相关工作](#related-work--相关工作)），成本更低：

```bash
python tools/cross_model_decode.py \
  --question "What is the units digit of 7^123?" \
  --source-model claude-opus-4-8 --decoder-model claude-haiku-4-5-20251001 \
  --compare-same-model
```

**Run the demo UI** instead:

或运行演示前端：

```bash
python server.py          # serves the demo on http://localhost:8000 / 在此地址提供演示
```

Open [http://localhost:8000](http://localhost:8000) and use either **Prove It Yourself** (paste a signature to
recover its sealed secret) or **Live Conversation** (chat, then "Unlock reasoning view").

打开 [http://localhost:8000](http://localhost:8000)，即可使用 **Prove It Yourself**（粘贴一个 signature 以还原其中封存的秘密）
或 **Live Conversation**（先对话，再点击「Unlock reasoning view」解锁推理视图）。

> If your environment lacks `ensurepip` / `venv`, install deps with
> `pip install --user --break-system-packages -r requirements.txt` instead.
>
> 若当前环境缺少 `ensurepip` / `venv`，可改用
> `pip install --user --break-system-packages -r requirements.txt` 安装依赖。



## API contract / API 接口约定


| Method & path / 方法与路径    | Purpose / 用途                                                                                                               |
| ------------------------ | -------------------------------------------------------------------------------------------------------------------------- |
| `GET /api/status`        | Per-IP daily quota + availability + running cost. / 按 IP 的每日配额、可用性与累计费用。                                                   |
| `GET /api/pricing`       | Per-model price table + total USD spent. / 各模型价格表与累计花费（USD）。                                                               |
| `GET /api/byok/template` | Memorize-prompt template + model/limits for the curl builder. / 用于构建 curl 命令的记忆提示词模板及模型/上限。                                |
| `POST /api/byok/unseal`  | Decode a submitted signature's bound model, replay it, return the recovered secret. / 解码提交的 signature 得到其绑定模型，回放并返回还原出的秘密。 |
| `POST /api/chat/send`    | Multi-turn chat; returns answer, summary, hidden-token count, `has_signature`. / 多轮对话；返回答案、摘要、隐藏 token 数、`has_signature`。  |
| `POST /api/chat/unseal`  | Replay a turn's signature to reveal the deeper reasoning trace. / 回放某一轮的 signature，揭示更深层的推理轨迹。                             |
| `POST /api/chat/reset`   | Clear a session. / 清空一个会话。                                                                                                 |




## Decode a signature by hand / 手动解码一个 signature

Pass a signature directly, or `@path` to read it from a file. Harvested signatures are
written to `logs/signatures/` when you run the server or the batch tool.

可以直接传入 signature，或用 `@路径` 从文件读取。运行服务端或批量工具时，采集到的
signature 会写入 `logs/signatures/`。

```bash
python tools/decode_signature.py @logs/signatures/<session>_turn0_<stamp>.txt
```



## Distill / Bedrock gateway + batch examples / Distill 网关与批量示例

This repo also speaks the **Distill** (`model-eval`) gateway. Full route / model /
availability details live in **[docs/distill-gateway.md](docs/distill-gateway.md)**.

本仓库也支持 **Distill**（`model-eval`）网关。完整的路由、模型与可用性说明见
**[docs/distill-gateway.md](docs/distill-gateway.md)**。

```bash
UPSTREAM_MODE=bedrock
UPSTREAM_BASE=http://api.model-eval.woa.com
UPSTREAM_PROVIDER=aws_third
DISTILL_APP_ID=your_app_id      # apply at https://model-eval.woa.com/model/apply
DISTILL_APP_KEY=your_app_key
MODEL=anthropic.claude-opus-4-8
EFFORT=max
```

The **provider** (`UPSTREAM_PROVIDER`) selects both the auth route and the wire protocol:

- `aws_third` (Bedrock): `POST {UPSTREAM_BASE}/model/{MODEL}/invoke`, body carries
`anthropic_version=bedrock-2023-05-31` (no top-level `model`).
- `anthropic` / `google` (native, e.g. Claude on Anthropic-direct / Vertex through Distill):
`POST {UPSTREAM_BASE}/v1/messages`, body carries `model`, header carries `anthropic-version`.

Auth is always `x-api-key: {APP_ID}:{APP_KEY}?provider={PROVIDER}&model={MODEL}`. The gateway
returns a thinking block (plaintext or summary) plus a `signature`; unseal still replays the
**signature alone**.

provider（`UPSTREAM_PROVIDER`）同时决定鉴权路由与协议：`aws_third` 走 Bedrock invoke，
`anthropic` / `google` 走 native `/v1/messages`。鉴权始终为
`x-api-key: {APP_ID}:{APP_KEY}?provider={PROVIDER}&model={MODEL}`。网关返回 thinking 块
（明文或摘要）与 `signature`；unseal 仍**仅回放 signature**。

### Model thinking profiles / 模型思考档位

The thinking request format is selected per model (override with `THINKING_MODE`):

thinking 请求格式按模型自动选择（可用 `THINKING_MODE` 覆盖）：

- **manual** (Sonnet 4.5, Opus 4.5, 3.7): `thinking:{type:enabled, budget_tokens:N}` — set `THINKING_BUDGET`.
- **adaptive** (Opus 5, Opus 4.8/4.7/4.6, Sonnet 5/4.6): `thinking:{type:adaptive, display}` + `output_config.effort` — set `EFFORT` (`low|medium|high|xhigh|max`). Manual `budget_tokens` returns HTTP 400.
- **always-on adaptive** (Fable 5, Mythos 5): same shape; thinking is always on, `effort` controls depth.

Examples / 示例:

```bash
# Opus 5 / Opus 4.8 at max effort (Bedrock route)
MODEL=anthropic.claude-opus-5   EFFORT=max python tools/make_example.py --limit 3
MODEL=anthropic.claude-opus-4-8 EFFORT=max python tools/make_example.py --limit 5
# Fable 5 — available on aws_third, also reachable via the native anthropic/google routes
MODEL=anthropic.claude-fable-5  EFFORT=max python tools/make_example.py --limit 1
# CLI overrides: --effort max --display summarized --thinking-mode adaptive
```

Note: Bedrock requires streaming when `max_tokens > 21333`. Fable 5 has stricter safety
classifiers than Opus and may return `stop_reason: "refusal"` on secret-planting prompts;
gateway `503`s are transient and usually clear on retry.

注意：Bedrock 在 `max_tokens > 21333` 时要求流式。Fable 5 的安全分类器比 Opus 严格，
对植入 secret 的提示可能返回 `stop_reason: "refusal"`；网关 `503` 属瞬时错误，重试通常可恢复。

If a Distill route uses a model marker that differs from the invoke-URL id, set
`DISTILL_MODEL_MARKER` (it is used only in the auth `?model=` query).

若 Distill 路由的 model marker 与 invoke URL 的 model id 不同，用 `DISTILL_MODEL_MARKER`
设置（仅用于鉴权 `?model=` 查询）。

Unit tests for the adapter: `python tools/test_thinking_adapter.py`.

### Batch-generate `examples/NN-*.md` / 批量生成示例

```bash
cd open-thinking-replay
set -a; source .env; set +a
python tools/make_example.py --dry-run          # preview plan
python tools/make_example.py                    # harvest → unseal → next free examples/NN-*.md
python tools/make_example.py --limit 1          # smoke one problem
python tools/make_example.py --budget 4000      # override thinking budget
```

Problems live in `[tools/problems.jsonl](tools/problems.jsonl)` (one JSON object per line:
`slug`, `question`, `answer`, `budget`, `max_tokens`, …). Each run writes:

题目列表见 `[tools/problems.jsonl](tools/problems.jsonl)`。每次运行会写出：

- `examples/NN-slug.md` — same write-up format as `04` / `05`
- `logs/signatures/` — harvested signatures
- `logs/unsealed/` — recovered CoT dumps
- `logs/events.jsonl` — structured cost / alignment events

A successful crack requires `planted secret ∈ recovered CoT` and
`planted secret ∉ visible reply`. On Bedrock, `gold_plaintext_alignment` compares the
signature-only recovery against the harvest thinking plaintext (never fed into the unseal
prompt). Treat that plaintext as a **summary**, not ground truth: it only equals the sealed
bytes when the reasoning is too short to summarise (see `ANALYSIS.md` §7). The reliable check
is the length oracle `ciphertext_len − len(recovered.encode("utf-8")) == 16`.

破解成功的判据：`planted secret` 出现在还原 CoT 中，且**不**出现在可见回答中。Bedrock 下
还会给出相对 harvest thinking 明文的 `gold_plaintext_alignment`（仅作校验，不参与 unseal
提示）。在 `claude-sonnet-4-6` 上该明文可与密封字节一致（见 `ANALYSIS.md` §7）；对齐偏低
通常表示引导提示省略/改写了内容，而不是 signature 封存了另一份文档。

## Configuration / 配置

All via environment variables (see `.env.example`): `UPSTREAM_BASE`, `UPSTREAM_KEY` /
`DISTILL_APP_ID`+`DISTILL_APP_KEY`, `UPSTREAM_MODE`, `MODEL`, `THINKING_BUDGET`,
`PORT`, `PROVE_DAILY`, `LIVE_DAILY`.

全部通过环境变量设置（见 `.env.example`）：`UPSTREAM_BASE`、`UPSTREAM_KEY` /
`DISTILL_APP_ID`+`DISTILL_APP_KEY`、`UPSTREAM_MODE`、`MODEL`、`THINKING_BUDGET`、
`PORT`、`PROVE_DAILY`、`LIVE_DAILY`。

## Notes & caveats / 说明与注意事项

- Under a **fixed verbatim** elicitor, replay of the same signature is deterministic
(byte-identical SHA256 on `claude-sonnet-4-6`; see `ANALYSIS.md` §7). The default
multi-strategy unseal path can still vary or drop self-corrections; refusals may occur
and usually succeed on retry with the same signature.

固定「逐字转录」引导提示时，同一 signature 的 replay 在 `claude-sonnet-4-6` 上可逐字节确定
（见 `ANALYSIS.md` §7）。仓库默认的多策略 unseal 仍可能省略自我纠正等内容；偶发拒绝时用同一
signature 重试通常即可。

- Calls go to the official Anthropic Messages API (`https://api.anthropic.com/v1/messages`)
authenticated with `x-api-key`; any Anthropic-compatible endpoint that forwards thinking
blocks should also work.

调用直接走 Anthropic 官方 Messages API（`https://api.anthropic.com/v1/messages`），
使用 `x-api-key` 鉴权；任何能转发 thinking 块的、兼容 Anthropic 的端点同样可用。

- Every call is logged under `LOG_DIR` (default `logs/`): `events.jsonl` (conversation,
signatures, recovered content, cost), `raw/` (raw request+response JSON), `signatures/`
(harvested signatures). Per-call and running cost is computed from token usage and also
returned in each API response and at `GET /api/pricing`.

每次调用都会记录到 `LOG_DIR`（默认 `logs/`）：`events.jsonl`（对话、signature、还原内容、
费用）、`raw/`（原始请求+响应 JSON）、`signatures/`（采集到的 signature）。单次与累计费用会
根据 token 用量计算，并在每个 API 响应及 `GET /api/pricing` 中返回。

- Quotas and sessions are in-memory (single process). Use a shared store for real deployment.

配额与会话均保存在内存中（单进程）。若要正式部署，请改用共享存储。

- **Never commit your** `.env`**.** Logs under `logs/` can contain harvested signatures and
recovered reasoning; both are gitignored by default.

**切勿提交** `.env`**。** `logs/` 下可能包含采集到的 signature 与还原内容，默认已被 gitignore。

### If you expose this server publicly / 若要公开部署

The demo is written for local, single-operator use. Before putting it on a public host,
be aware of exactly what it stores:

本项目是为本地单人使用而写的。公开部署前请先了解它到底存了什么：

- Every log event records the client IP (`log_event` → `"ip"`), so `logs/events.jsonl`
becomes a visitor log.

每条日志事件都会记录访客 IP，因此 `logs/events.jsonl` 实质上是一份访问日志。

- `POST /api/byok/unseal` writes the **recovered plaintext** to `events.jsonl`
(`recovered_secret`); `POST /api/chat/unseal` writes the recovered trace
(`unsealed_cot`) plus a copy under `logs/unsealed/`.

`/api/byok/unseal` 会把**还原出的明文**写入 `events.jsonl`；`/api/chat/unseal` 会写入
还原轨迹并在 `logs/unsealed/` 留副本。

- The submitted `signature` itself is persisted under `logs/signatures/`.

提交的 `signature` 本身会落盘到 `logs/signatures/`。

The BYOK flow is designed so your plaintext secret never reaches this server — you run the
harvest `curl` yourself and hand over only the signature. But the **recovered** value is
produced and logged server-side. Tell your users that, keep them to random throwaway test
strings, and add access control, log retention limits and a privacy notice before exposing
the service. Never submit real passwords, API keys, personal data or confidential material.

BYOK 流程的设计确保你的明文秘密不会到达本服务器——harvest 的 `curl` 由你自己执行，只交出
signature。但**还原出的**内容是在服务端产生并被记录的。请如实告知使用者、只允许提交随机的
一次性测试字符串，并在对外开放前加上访问控制、日志保留期限与隐私声明。切勿提交真实密码、
API key、个人信息或机密材料。

## Repository layout / 仓库结构

```
├── server.py                 Flask backend (harvest / replay / quotas / cost)
├── static/index.html         vendored demo frontend (see Attribution)
├── docs/distill-gateway.md   Distill gateway routes, models, thinking parameters
├── docs/reproduction-log.md  historical Opus 5 / 4.8 / Fable 5 Distill run log
├── examples/                 reproduced write-ups (question → visible answer → recovered CoT)
├── tools/
│   ├── extract_cot.py        verified CoT extraction (recommended entry point)
│   ├── extract_pipeline_capture.py  batch extraction from an external agent harness's capture log
│   ├── thinking_extract_worker.py   continuous cost-tiered worker built on the above
│   ├── cross_model_decode.py        cross-model decoding (arXiv:2608.09867 reproduction)
│   ├── decode_signature.py   standalone protobuf decoder for any signature
│   ├── make_example.py       batch harvest → unseal → write examples/NN-*.md
│   ├── problems.jsonl        problem set for the batch tool
│   ├── test_thinking_adapter.py  unit tests for the adapter and the length oracle
│   ├── test_extract_pipeline_capture.py  unit tests for capture chaining + prefix selection
│   └── test_cross_model_decode.py   unit tests for cross-model transcription post-processing
├── ANALYSIS.md               signature wire format + empirical findings
└── .env.example              configuration template (no credentials)
```



## Attribution / 致谢

Based on the original **[Open Reasoning](https://github.com/s-JoL/open-reasoning)** project
by s-JoL, which published the demo and unlocked examples but not the recovery
implementation. The repository was reachable when this work began and now returns 404.
`static/index.html` is a vendored copy of its demo frontend, included for reproducibility;
copyright remains with its original author.

本仓库基于 s-JoL 的原始 **[Open Reasoning](https://github.com/s-JoL/open-reasoning)** 项目，
该项目公开了演示与解锁样例，但未公开还原实现。本工作开始时该仓库可访问，现返回 404。
`static/index.html` 是其演示前端的 vendored 副本，仅为复现目的收录，版权归原作者所有。

The upstream project grants no explicit licence, so default copyright applies to the
inherited files and the MIT grant in [LICENSE](LICENSE) covers only this repository's own
additions. If you are the upstream author and want attribution changed or material removed,
please open an issue.

上游项目未授予明确许可证，继承文件适用默认著作权，[LICENSE](LICENSE) 中的 MIT 授权仅覆盖本
仓库的原创新增部分。如您是上游作者，希望调整署名或移除内容，请提交 issue。

## Disclaimer / 免责声明

This is an independent research reproduction, not affiliated with or endorsed by Anthropic
or any model provider. It uses only documented public API behavior — extended thinking and
the documented multi-turn protocol of passing `thinking` blocks (including their
`signature`) back to the API. It does not break, bypass, or weaken any cryptography: the
sealed ciphertext is decrypted **by the provider**, server-side, exactly as the multi-turn
protocol specifies. Use it in accordance with your provider's terms of service, and only
with accounts and models you are authorised to use.

本项目是独立的研究复现，与 Anthropic 或任何模型服务商无从属或背书关系。它仅使用有文档记载的
公开 API 行为——扩展思考，以及把 `thinking` 块（含其 `signature`）回传给 API 的多轮协议。
它不破解、不绕过、不削弱任何密码学机制：被封存的密文是**由服务商在服务端**按多轮协议正常解密的。
请在遵守服务商条款的前提下使用，且只用于你有权使用的账号与模型。

## License / 许可证

[MIT](LICENSE) © 2026
