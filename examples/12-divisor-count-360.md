# Example: divisor count 360 — answer-only harvest, thinking recovered byte-exactly from the signature

Reproduced with open-thinking-replay. **Answer-only construction**: the visible reply is
forced down to the bare final result, so *no* reasoning is human-readable in the response —
the chain-of-thought survives only inside the sealed `signature`. Recovering that
signature back into the working (plus the planted secret, which the visible reply never
contains) is the proof that the signature encodes the hidden reasoning.
Planted marker uses open-reasoning's BYOK secret shape `WORD-XXXXX-WORD-NN` (same
`rollSecret` construction as `index.html`).
Based on: https://github.com/s-JoL/open-reasoning

Model: `anthropic.claude-opus-5` (requested) · provider `aws_third` (Bedrock invoke) · `thinking={type:adaptive, display:omitted}` · `effort=max` · `max_tokens=16000` · plant_secret = `COMET-XAQZF-RIVER-99`

Q:
```markdown
How many positive divisors does 360 have?
```

## What the caller actually sees (visible reply — result only, 2 chars)

```markdown
24
```

Summary field (`display:omitted` → empty; 209 hidden reasoning tokens sealed in signature):

```markdown
*(empty — sealed into signature only)*
```

## Recovered thinking (from the signature alone)

The replay turn carries the **bare question** plus `{"type":"thinking","thinking":"","signature":…}`
and a fixed verbatim-transcription elicitor. The elicitor never names the secret, so the
secret exists only inside the sealed blob.

- signature = 696 base64 chars · ciphertext (protobuf field `#5`) = 320 bytes
- recovered = 298 chars / 304 UTF-8 bytes
- **`ciphertext_len − recovered_bytes = 16`** — exactly the AES-GCM authentication tag, i.e. the
  recovery is byte-identical to the sealed plaintext
- signature header field `#6` binds the internal codename `claude-honey`, not the public model id — decode it with `python tools/decode_signature.py`
- planted secret in recovered = **True** · in visible reply = **False**
- **no-signature control**: same request with the thinking block removed returned
  2 chars and did **not** contain the secret → the secret came from the signature,
  not from the prompt

```markdown
COMET-XAQZF-RIVER-99

360 = 2^3 · 3^2 · 5^1. Divisors: (3+1)(2+1)(1+1) = 4·3·2 = 24.

Verification: divisors of 360: 1,2,3,4,5,6,8,9,10,12,15,18,20,24,30,36,40,45,60,72,90,120,180,360 — that's 24.

Count: 1,2,3,4,5,6,8,9,10,12 (10), 15,18,20,24,30,36,40,45,60,72 (20), 90,120,180,360 (24). Yes, 24.
```

Ground truth: expected answer = `24` · model visible = `24`.
