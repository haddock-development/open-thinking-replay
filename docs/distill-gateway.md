# Distill Gateway — Claude thinking API / Distill 网关调用说明

How this repo talks to Claude through the Distill (`model-eval`) gateway, which routes
are available, and which thinking parameters each model accepts.

本仓库如何通过 Distill（`model-eval`）网关调用 Claude，包含可用路由与各模型接受的
thinking 参数。

> Credentials are never stored in this repo. Put your own `DISTILL_APP_ID` /
> `DISTILL_APP_KEY` in a local `.env` (gitignored).
>
> 本仓库不保存任何凭证。请把自己的 `DISTILL_APP_ID` / `DISTILL_APP_KEY` 放进本地
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

| Provider       | Endpoint                           | Body carries         | Version travels in                          |
| -------------- | ----------------------------------- | --------------------- | -------------------------------------------- |
| `aws_third`    | `POST {BASE}/model/{MODEL}/invoke` | no top-level `model` | body `anthropic_version=bedrock-2023-05-31` |
| `anthropic`    | `POST {BASE}/v1/messages`          | `model`               | header `anthropic-version`                   |
| `google`       | `POST {BASE}/v1/messages`          | `model`               | header `anthropic-version`                   |
| `naci_default` | `POST {BASE}/v1/messages`          | `model`               | header `anthropic-version`                   |

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

| Status | Message                                         | Meaning                                                                                                                                                                     |
| ------ | ------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `402`  | `the provider (provider=X) does not exist`      | provider name is not a valid gateway id — a typo, e.g. `naci` instead of `naci_default`                                                                                     |
| `402`  | `your APP_ID(...) has insufficient budget: ¥-N` | provider is provisioned for the key; the budget for it is exhausted. Budget is tracked **per provider**, so one provider can be negative while another still serves traffic |
| `500`  | `构建候选集失败: no available account selected`        | provider name is valid, but no upstream account is bound to this **(provider, model) pair** for this key. Not transient and not a budget problem — retrying does not help   |
| `503`  | `Bedrock is unable to process your request`     | transient upstream fault; retrying the identical body often succeeds                                                                                                        |

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

| Profile              | Models                                       | Request shape                                                |
| --------------------- | ---------------------------------------------- | ---------------------------------------------------------------- |
| `manual`             | Sonnet 4.5, Opus 4.5, Claude 3.7             | `thinking:{type:enabled, budget_tokens:N}`                   |
| `adaptive`           | Opus 5, Opus 4.8 / 4.7 / 4.6, Sonnet 5 / 4.6 | `thinking:{type:adaptive, display}` + `output_config.effort` |
| `always_on_adaptive` | Fable 5, Mythos 5                            | same as adaptive; thinking cannot be disabled                |

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

## 4. Availability model / 可用性模型

All four provider names below are part of the gateway's documented, supported surface —
with correct account provisioning and a positive budget on the relevant provider, every
route in the table works exactly as specified in §2. Real-time reachability of a specific
`(provider, model)` pair is a property of **your own Distill account's provisioning and
budget**, not of this repository or of the gateway's design; use §2's status-code table
to tell a provisioning gap (`500`) from a budget gap (`402`) from a transient fault (`503`)
on your own key, rather than assuming this doc's authors' account state applies to yours.

以下四个 provider 名均属于网关文档化、受支持的能力面——在账号已正确开通且相应 provider 预算
为正的前提下，§2 表中列出的每条路由都会按规范工作。某个 `(provider, model)` 组合此刻是否可达，
取决于**你自己 Distill 账号的开通与预算状态**，与本仓库或网关设计本身无关；请用 §2 的状态码表
在自己的 key 上区分开通缺口（`500`）、预算缺口（`402`）与瞬时故障（`503`），而不要把本文作者
账号当时的状态当作对你同样成立的结论。

| Provider       | Wire           | Documented coverage                                                             |
| -------------- | -------------- | ---------------------------------------------------------------------------------- |
| `aws_third`    | Bedrock invoke | Opus 5, Opus 4.8, Fable 5 (rotates across upstream accounts of varying health — see below) |
| `anthropic`    | native         | Opus 5, Opus 4.8, Fable 5                                                          |
| `google`       | native         | Fable 5 (and other Vertex-routed Claude models, account-dependent)                 |
| `naci_default` | native         | Sonnet 4.6 confirmed end-to-end; Opus 4.6 / 4.7 are listed in `/v1/models` but require separate provisioning on this provider (see below) |

Two operational properties are worth planning a batch runner around, independent of any
single account's state:

以下两条运行属性与具体账号状态无关，批处理设计时应当考虑：

- **Provider-level load balancing.** Even on a funded, provisioned `(provider, model)` pair,
  the gateway distributes calls across multiple upstream accounts of differing health, so an
  individual call can still return a transient `402`/`429`/`503` that clears on retry.
  **Any batch runner needs retry with backoff**, and should not treat a single failure as
  terminal — see [`tools/extract_cot.py`](../tools/extract_cot.py)'s retry loop for a
  reference implementation.

  即使在预算充足、已开通的 `(provider, model)` 组合上，网关也会在健康状况不同的多个上游账号
  之间做负载均衡，单次调用仍可能返回瞬时的 `402`/`429`/`503`，重试通常可恢复。**任何批处理都
  需要带退避的重试**，不要把单次失败当作终态——参考实现见
  [`tools/extract_cot.py`](../tools/extract_cot.py) 的重试循环。

- **Budget is tracked per provider, independently.** A key can be negative on one provider
  while fully funded on another; a `402` on `google` says nothing about whether `aws_third`
  or `anthropic` will work for the same key.

  预算**按 provider 分别计算**：同一个 key 可以在某个 provider 上欠费，同时在另一个 provider
  上预算充足；`google` 上的 `402` 不代表同一个 key 在 `aws_third` 或 `anthropic` 上也会失败。

### `naci_default` provisioning note / `naci_default` 开通说明

On this provider, `claude-sonnet-4-6` is confirmed to work end to end (harvest returns a
`signature` bound to `claude-sonnet-4-6`; replay recovers the sealed reasoning). The Opus
4.6 / 4.7 model ids are listed in the gateway's `/v1/models` catalogue (`owned_by:
vertex-ai`, with `anthropic` among their `supported_endpoint_types`) but returned
`500 no available account selected` in our testing across every wire shape, model-id
variant, and endpoint we tried — a `(naci_default, claude-opus-4-*)` **provisioning gap**
specific to that account/provider pairing, not a wire-shape or model-id error. Request
provisioning for that pair from your Distill administrator if you need it, or use
`aws_third` / `anthropic` for Opus 4.6 / 4.7 instead.

该 provider 下 `claude-sonnet-4-6` 端到端确认可用（harvest 返回绑定 `claude-sonnet-4-6` 的
`signature`，replay 能还原封存的推理）。Opus 4.6 / 4.7 的 model id 出现在网关 `/v1/models`
清单中（`owned_by: vertex-ai`，`supported_endpoint_types` 含 `anthropic`），但我们测试的每种
wire 形态、model id 写法与端点组合都返回 `500 no available account selected`——这是
`(naci_default, claude-opus-4-*)` 这一特定账号/provider 组合的**开通缺口**，不是 wire 形态或
model id 的问题。如需要该组合，请向你的 Distill 管理员申请开通，或改用 `aws_third` /
`anthropic` 调用 Opus 4.6 / 4.7。

### Model-specific behavior worth knowing / 值得了解的模型特性

- **Fable 5 has stronger safety classifiers than Opus, firing in two distinct places.**
  A prompt that asks the model to hide a string in its reasoning can return
  `stop_reason: "refusal"` with `stop_details.category: "cyber"`. A replay that asks for a
  verbatim transcription of the private reasoning returns
  `stop_details.category: "reasoning_extraction"` with an **empty** `content` **array** —
  there is no text block to parse. Handle an empty `content` on HTTP 200 as a terminal
  refusal rather than a retryable fault. See [reproduction-log.md](reproduction-log.md) §4d.

  Fable 5 的安全分类器在两处触发：要求把字符串藏进推理的提示可能返回
  `category: "cyber"`；要求逐字转录私有推理的 replay 返回 `category: "reasoning_extraction"`
  且 `content` 为**空数组**。应把 HTTP 200 且 `content` 为空视作终态拒绝，而非可重试故障。

- **Bedrock requires streaming when `max_tokens > 21333`.**

  Bedrock 在 `max_tokens > 21333` 时要求流式调用。

- **A `503` from the gateway is transient** — retrying the identical body often succeeds.

  网关返回的 `503` 是瞬时的，用相同请求体重试通常可成功。

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
