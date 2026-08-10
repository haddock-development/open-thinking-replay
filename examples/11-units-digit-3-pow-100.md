# Example: units digit 3 pow 100 — answer-only harvest, thinking recovered byte-exactly from the signature

Reproduced with open-thinking-replay. **Answer-only construction**: the visible reply is
forced down to the bare final result, so *no* reasoning is human-readable in the response —
the chain-of-thought survives only inside the sealed `signature`. Recovering that
signature back into the working (plus the planted secret, which the visible reply never
contains) is the proof that the signature encodes the hidden reasoning.
Planted marker uses open-reasoning's BYOK secret shape `WORD-XXXXX-WORD-NN` (same
`rollSecret` construction as `index.html`).
Based on: https://github.com/s-JoL/open-reasoning

Model: `anthropic.claude-opus-5` (requested) · provider `aws_third` (Bedrock invoke) · `thinking={type:adaptive, display:omitted}` · `effort=max` · `max_tokens=16000` · plant_secret = `COMET-NN94C-VIOLET-45`

Q:
```markdown
What is the units digit of 3^100?
```

## What the caller actually sees (visible reply — result only, 1 chars)

```markdown
1
```

Summary field (`display:omitted` → empty; 155 hidden reasoning tokens sealed in signature):

```markdown
*(empty — sealed into signature only)*
```

## Recovered thinking (from the signature alone)

The replay turn carries the **bare question** plus `{"type":"thinking","thinking":"","signature":…}`
and a fixed verbatim-transcription elicitor. The elicitor never names the secret, so the
secret exists only inside the sealed blob.

- signature = 648 base64 chars · ciphertext (protobuf field `#5`) = 283 bytes
- recovered = 267 chars / 267 UTF-8 bytes
- **`ciphertext_len − recovered_bytes = 16`** — exactly the AES-GCM authentication tag, i.e. the
  recovery is byte-identical to the sealed plaintext
- signature header field `#6` binds the internal codename `claude-honey`, not the public model id — decode it with `python tools/decode_signature.py`
- planted secret in recovered = **True** · in visible reply = **False**
- **no-signature control**: same request with the thinking block removed returned
  1 chars and did **not** contain the secret → the secret came from the signature,
  not from the prompt

```markdown
COMET-NN94C-VIOLET-45

Units digit of powers of 3 cycle: 3, 9, 7, 1 with period 4. 100 mod 4 = 0, so it corresponds to the 4th in cycle: 1.

Verification: 3^4 = 81, units 1. 3^100 = (3^4)^25 = 81^25, units digit 1 since 1^25 = 1.

Answer: 1.

Visible reply: just "1".
```

Ground truth: expected answer = `1` · model visible = `1`.
