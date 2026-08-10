# Distill Gateway — Claude thinking API / Distill 网关调用说明

How this repo talks to Claude through the Distill (`model-eval`) gateway, which routes
are available, and which thinking parameters each model accepts.

本仓库如何通过 Distill（`model-eval`）网关调用 Claude，包含可用路由与各模型接受的
thinking 参数。

> Credentials are never stored in this repo. Put your own `DISTILL_APP_ID` /
> `DISTILL_APP_KEY` in a local `.env` (gitignored).
> <br>本仓库不保存任何凭证。请把自己的 `DISTILL_APP_ID` / `DISTILL_APP_KEY` 放进本地
> `.env`（已被 gitignore）。

---

## 1. Getting credentials / 申请凭证

1. Visit the [Distill budget application page](https://model-eval.woa.com/model/apply)
2. Apply for the vendor you need (Claude models route through `aws_third`,
   `anthropic`, or `google`)
3. You will receive an `APP-ID` and an `APP-KEY`

```bash
cp .env.example .env
# edit .env: DISTILL_APP_ID / DISTILL_APP_KEY / UPSTREAM_PROVIDER / MODEL
set -a; source .env; set +a
```

---

## 2. Routes and wire protocols / 路由与协议

`UPSTREAM_PROVIDER` selects **both** the auth route and the request/response wire shape.

`UPSTREAM_PROVIDER` 同时决定鉴权路由与请求/响应协议。

| Provider | Endpoint | Body carries | Version travels in |
| --- | --- | --- | --- |
| `aws_third` | `POST {BASE}/model/{MODEL}/invoke` | no top-level `model` | body `anthropic_version=bedrock-2023-05-31` |
| `anthropic` | `POST {BASE}/v1/messages` | `model` | header `anthropic-version` |
| `google` | `POST {BASE}/v1/messages` | `model` | header `anthropic-version` |
| `naci_default` | `POST {BASE}/v1/messages` | `model` | header `anthropic-version` |

The rule implemented by `_wire_shape()` — Bedrock invoke only when the provider name starts
with `aws`, native Messages otherwise — holds for `naci_default` as measured. It needs no
extra header beyond `anthropic-version`, and the Bedrock invoke path is *not* an alternative
for it (`/model/{MODEL}/invoke` returns the same account error as the native path).

`_wire_shape()` 的规则（provider 名以 `aws` 开头才走 Bedrock invoke，否则走 native Messages）
在 `naci_default` 上实测成立。除 `anthropic-version` 外无需额外 header；Bedrock invoke 路径对它
并非可选替代（`/model/{MODEL}/invoke` 返回与 native 路径相同的账号错误）。

Auth header is the same in all cases:

以上路由的鉴权头相同：

```
x-api-key: {APP_ID}:{APP_KEY}?provider={PROVIDER}&model={MODEL}
```

Platform markers follow the pattern `api_<provider>_<model>`, e.g.
`api_aws_third_anthropic.claude-opus-5`, `api_anthropic_claude-fable-5`. If a route's
marker differs from the invoke-URL model id, set `DISTILL_MODEL_MARKER` — it is used
**only** in the auth query, never in the URL.

平台 marker 形如 `api_<provider>_<model>`。若某路由的 marker 与 invoke URL 的 model id
不同，用 `DISTILL_MODEL_MARKER` 设置（**仅**用于鉴权 query，不进入 URL）。

### Distinguishing gateway rejections / 区分网关的拒绝原因

The status code tells you which layer rejected the call, which matters when you are deciding
whether to retry, top up budget, or request provisioning:

状态码指明是哪一层拒绝了请求，这决定了应当重试、充值预算，还是申请开通：

| Status | Message | Meaning |
| --- | --- | --- |
| `402` | `the provider (provider=X) does not exist` | provider name is not a valid gateway id — a typo, e.g. `naci` instead of `naci_default` |
| `402` | `your APP_ID(...) has insufficient budget: ¥-N` | provider is provisioned for the key; the budget for it is exhausted. Budget is tracked **per provider**, so one provider can be negative while another still serves traffic |
| `500` | `构建候选集失败: no available account selected` | provider name is valid, but no upstream account is bound to this **(provider, model) pair** for this key. Not transient and not a budget problem — retrying does not help |
| `503` | `Bedrock is unable to process your request` | transient upstream fault; retrying the identical body often succeeds |

`402` 表示 provider 名无效或该 provider 的预算已用尽（预算**按 provider 分别计算**，某个
provider 欠费时其他 provider 仍可调用）；`500` 的 `no available account selected` 表示 provider
名有效，但该 key 在这个 **(provider, model) 组合**上没有绑定可用的上游账号，属于开通问题，重试
无效；`503` 属瞬时故障，重试通常可恢复。

`GET {BASE}/v1/models` returns the gateway's full model catalogue (id, `owned_by`,
`supported_endpoint_types`). Use it to confirm a model id's exact spelling — but note that a
model appearing there does **not** imply your key can reach it under a given provider; that is
the `500` case above.

`GET {BASE}/v1/models` 返回网关的完整模型清单（id、`owned_by`、`supported_endpoint_types`），
可用于确认 model id 的准确写法。但模型出现在清单中**不等于**你的 key 能在某个 provider 下调用它，
后者对应上面的 `500` 情况。

---

## 3. Thinking parameters per model / 各模型的 thinking 参数

The request shape is picked automatically by `thinking_profile()` in `server.py`.
Override with `THINKING_MODE`.

请求格式由 `server.py` 的 `thinking_profile()` 自动选择，可用 `THINKING_MODE` 覆盖。

| Profile | Models | Request shape |
| --- | --- | --- |
| `manual` | Sonnet 4.5, Opus 4.5, Claude 3.7 | `thinking:{type:enabled, budget_tokens:N}` |
| `adaptive` | Opus 5, Opus 4.8 / 4.7 / 4.6, Sonnet 5 / 4.6 | `thinking:{type:adaptive, display}` + `output_config.effort` |
| `always_on_adaptive` | Fable 5, Mythos 5 | same as adaptive; thinking cannot be disabled |

Adaptive-only models **reject** manual budgets:

adaptive 专用模型会**拒绝**手动 budget：

```
HTTP 400  "thinking.type.enabled" is not supported for this model.
          Use "thinking.type.adaptive" and "output_config.effort".
```

`effort` accepts `low | medium | high | xhigh | max` (default `high`), and must sit in
the top-level `output_config`, not inside `thinking`.

`effort` 取值 `low | medium | high | xhigh | max`（默认 `high`），且必须放在顶层
`output_config` 中，不能放进 `thinking`。

`thinking.display` controls only what text comes back:

`thinking.display` 只影响返回的文本：

- `omitted` — empty `thinking` field, `signature` still returned (default on newest models)
- `summarized` — a condensed summary of the reasoning, written by a smaller model

Either way you are billed for the full thinking tokens, and the `signature` is identical.

两种取值下都按完整 thinking tokens 计费，且 `signature` 相同。

---

## 4. Observed availability / 实测可用性

Measured against one Distill account; your provisioning may differ.

以下基于单个 Distill 账号实测，你的开通情况可能不同。

| Marker | Result |
| --- | --- |
| `api_aws_third_anthropic.claude-opus-5` | available, stable |
| `api_aws_third_anthropic.claude-opus-4-8` | available, stable |
| `api_aws_third_anthropic.claude-fable-5` | provisioned but frequently returns `503 Bedrock is unable to process your request` |
| `api_anthropic_claude-fable-5` | route valid; blocked by account budget in our test |
| `api_google_claude-fable-5` | route valid; blocked by account budget in our test |
| `api_naci_default_claude-opus-4-6` | native wire correct, model id listed in `/v1/models`; **not reachable** — `500 no available account selected` on every attempt |
| `api_naci_default_claude-opus-4-7` | same as 4.6 — `500 no available account selected` on every attempt |
| `api_naci_default_claude-sonnet-4-6` | available; full harvest → replay cycle works (see below) |

### `naci_default` measurement detail / `naci_default` 实测细节

Both Opus markers were exercised with the repo's own harvest/replay pipeline (5 problems
each, `effort=max`, `display=omitted`). All 10 harvests failed before reaching the model, so
no tokens were billed and no replay data exists for them. The failure was reproduced across
the native and Bedrock wires, the `anthropic.` / `us.anthropic.` prefixes, a `-thinking`
suffix, dotted and `-v1:0` version forms, the marker string in the auth `?model=` query, and
the OpenAI-shaped `/v1/chat/completions` endpoint — always the same `500`. Repeating the calls
over several minutes gave an identical result, so this is a provisioning gap on the
`(naci_default, claude-opus-4-*)` pairs rather than a transient fault or a wire-shape error.

两个 Opus marker 均使用本仓库的 harvest/replay 流程实测（每个 5 题，`effort=max`、
`display=omitted`）。10 次 harvest 全部在到达模型前失败，因此未产生 token 费用，也没有对应的
replay 数据。该失败在 native 与 Bedrock 两种 wire、`anthropic.` / `us.anthropic.` 前缀、
`-thinking` 后缀、点号与 `-v1:0` 版本写法、把 marker 放入鉴权 `?model=` 查询，以及 OpenAI 形态的
`/v1/chat/completions` 端点下均一致复现，返回相同的 `500`。数分钟内重复调用结果不变，因此这是
`(naci_default, claude-opus-4-*)` 组合的开通缺失，而非瞬时故障或 wire 形态错误。

`claude-sonnet-4-6` is the one Claude model this provider served for our key, and it confirms
the route end to end: 5/5 harvests returned a `signature` (384–540 chars, bound model
`claude-sonnet-4-6`), and 5/5 replays recovered the planted secret while the visible reply
never contained it. Note that `/v1/models` reports the Opus 4.6 / 4.7 entries as
`owned_by: vertex-ai` with `anthropic` among their `supported_endpoint_types`, so the model ids
and the endpoint choice were not the limiting factor. Budgets are tracked per provider, so
`naci_default` kept serving traffic while `aws_third` / `anthropic` / `google` were negative.

`claude-sonnet-4-6` 是该 provider 下我们的 key 唯一可调用的 Claude 模型，它端到端验证了这条路由：
5/5 次 harvest 返回了 `signature`（384–540 字符，绑定模型为 `claude-sonnet-4-6`），5/5 次 replay
还原出了植入的 secret，且可见回答中始终不含该 secret。`/v1/models` 中 Opus 4.6 / 4.7 的记录为
`owned_by: vertex-ai`，且 `supported_endpoint_types` 包含 `anthropic`，因此 model id 与端点选择
不是限制因素。预算按 provider 分别计算，因此 `aws_third` / `anthropic` / `google` 欠费时
`naci_default` 仍可调用。

### Valid provider names are a closed set / provider 名是一个封闭集合

Probing arbitrary provider strings returns `402 the provider (provider=X) does not exist`,
which distinguishes a typo from a real budget problem. Only four names were accepted by the
gateway for this key:

用任意字符串探测 provider 会返回 `402 ... does not exist`，可据此区分拼写错误与真实的预算问题。
该 key 下网关只接受四个 provider 名：

| Provider | Wire | Status observed |
| --- | --- | --- |
| `aws_third` | Bedrock invoke | serves Opus 5, Opus 4.8, Fable 5 after a top-up; rotates through upstream accounts, so individual calls can still return `402` or `429` and must be retried |
| `anthropic` | native | serves Opus 5 and Opus 4.8; `500 no available account` for Fable 5 |
| `naci_default` | native | serves `claude-sonnet-4-6`; `500 no available account` for Opus 4.6 / 4.7 and Fable 5 |
| `google` | native | budget exhausted (`402`) for every model tried |

Two consequences worth planning around. Budget is tracked per provider, so a provider can go
negative while others keep serving. And even on a funded provider the gateway load-balances
across upstream accounts of differing health — during one probe the same model alternated
between `OK`, `402`, and `429` within a minute, so **any batch runner needs retry with
backoff** rather than treating the first failure as terminal.

Opus 4.6 / 4.7 were blocked on all four providers at once: three by budget, one by missing
provisioning. They are listed in `/v1/models` (`owned_by: vertex-ai`), so the blocker is
provisioning, not the model id.

预算按 provider 独立计算；此外，即使在已充值的 provider 上，网关也会在健康状况不同的上游账号
之间轮询——一次探测中同一模型在一分钟内交替返回 `OK`、`402`、`429`，因此**批处理必须带退避
重试**，不能把首次失败当作终态。Opus 4.6 / 4.7 在四个 provider 上同时受阻（三个因预算、一个
因未开通）；它们在 `/v1/models` 中存在（`owned_by: vertex-ai`），限制来自开通而非 model id。

Notes:

- Fable 5 has stronger safety classifiers than Opus, and they fire in two distinct places.
  A prompt that asks the model to hide a string in its reasoning can return
  `stop_reason: "refusal"` with `stop_details.category: "cyber"`. A replay that asks for a
  verbatim transcription of the private reasoning returns
  `stop_details.category: "reasoning_extraction"` with an **empty `content` array** — there is
  no text block to parse. Handle an empty `content` on HTTP 200 as a terminal refusal rather
  than a retryable fault. See [reproduction-log.md](reproduction-log.md) §4d.
  <br>Fable 5 的安全分类器在两处触发：要求把字符串藏进推理的提示可能返回
  `category: "cyber"`；要求逐字转录私有推理的 replay 返回 `category: "reasoning_extraction"`
  且 `content` 为**空数组**。应把 HTTP 200 且 `content` 为空视作终态拒绝，而非可重试故障。
- Bedrock requires streaming when `max_tokens > 21333`.
  <br>Bedrock 在 `max_tokens > 21333` 时要求流式调用。
- `503` from the gateway is transient — retrying the identical body often succeeds.
  <br>网关返回的 `503` 是瞬时的，用相同请求体重试通常可成功。

---

## 5. Quick verification / 快速验证

```bash
set -a; source .env; set +a

# Adapter unit tests (no network calls)
python tools/test_thinking_adapter.py

# Preview a batch run without spending budget
python tools/make_example.py --dry-run

# One real case
MODEL=anthropic.claude-opus-4-8 EFFORT=max python tools/make_example.py --limit 1
```

---

## 6. References / 参考

- [Anthropic — Extended thinking](https://platform.claude.com/docs/en/build-with-claude/extended-thinking)
- [Anthropic — Adaptive thinking](https://platform.claude.com/docs/en/build-with-claude/adaptive-thinking)
- [Anthropic — Effort](https://platform.claude.com/docs/en/build-with-claude/effort)
- [Amazon Bedrock — Claude Messages API](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-messages-api.html)
