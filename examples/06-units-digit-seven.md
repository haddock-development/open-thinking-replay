# Example: Units digit of 7^2024 — answer-only harvest, thinking reversed from signature

Reproduced with open-thinking-replay. **Answer-only construction**: the visible reply is
forced down to the bare final result, so *no* reasoning is human-readable in the response —
the full chain-of-thought survives only inside the sealed `signature`. Reversing that
signature back into the working (plus the planted secret, which the visible reply never
contains) is the actual proof that the signature encodes the hidden reasoning.
Planted marker uses open-reasoning's BYOK secret shape `WORD-XXXXX-WORD-NN` (same
`rollSecret` construction as `index.html`). `<cot>` is only our local dump wrapper.
Based on: https://github.com/s-JoL/open-reasoning

Model: anthropic.claude-sonnet-4-5-20250929-v1:0 · mode=`bedrock` · `thinking.budget_tokens=2048` · `max_tokens=4096` · plant_secret = `QUARTZ-3QPTG-VIOLET-87`

Q:
```markdown
Find the units digit of $7^{2024}$.
```

## What the caller actually sees (visible reply — result only, 1 chars)

```markdown
1
```

Summary field (Bedrock returns plaintext thinking (kept server-side for gold check); 596 hidden reasoning tokens sealed in signature):

```markdown
*(Bedrock Distill returned thinking plaintext alongside the signature; that plaintext is used **only** as ground-truth for alignment below. The reverse-engineered block is recovered from the signature alone.)*
```

## Reverse-engineered thinking (recovered from the signature alone)

- **token_alignment = 0.54** (323/596 recovered/hidden)
- **gold_plaintext_alignment = 0.36** (SequenceMatcher vs harvest thinking plaintext; signature-only recovery)
- recovered_chars = 705 · reversed CoT is **705x** longer than the visible reply
- planted secret present in reversed CoT = **True** · present in visible reply = **False**
  (secret appears only in the recovered thinking → the signature really carried the hidden reasoning)
- unseal_ok = **True** · meta = {'audit_token': 'QUARTZ-3QPTG-VIOLET-87', 'chunks': 1, 'recovered_output_tokens': 323, 'strategy': 'cot', 'truncated': False}

```markdown
QUARTZ-3QPTG-VIOLET-87

To find the units digit of $7^{2024}$, I need to find the pattern of units digits for powers of 7.

$7^1 = 7$ (units digit: 7)
$7^2 = 49$ (units digit: 9)
$7^3 = 343$ (units digit: 3)
$7^4 = 2401$ (units digit: 1)
$7^5 = 16807$ (units digit: 7)
$7^6 = 117649$ (units digit: 9)

The pattern of units digits is: 7, 9, 3, 1, 7, 9, 3, 1, ...

The pattern repeats every 4 powers.

To find which position 2024 corresponds to in this cycle, I need to find $2024 \mod 4$.

$2024 = 4 \times 506 + 0$

So $2024 \equiv 0 \pmod{4}$.

When the exponent is divisible by 4, we're at position 4 in the cycle, which corresponds to a units digit of 1.

Therefore, the units digit of $7^{2024}$ is 1.
```

Ground truth: expected answer = `1` · model visible = `1`.

Cost: send $0.009591 + reverse $0.005745 · 26.2s.
