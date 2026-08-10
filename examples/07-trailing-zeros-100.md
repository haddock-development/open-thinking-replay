# Example: Trailing zeros of 100! — answer-only harvest, thinking reversed from signature

Reproduced with open-thinking-replay. **Answer-only construction**: the visible reply is
forced down to the bare final result, so *no* reasoning is human-readable in the response —
the full chain-of-thought survives only inside the sealed `signature`. Reversing that
signature back into the working (plus the planted secret, which the visible reply never
contains) is the actual proof that the signature encodes the hidden reasoning.
Planted marker uses open-reasoning's BYOK secret shape `WORD-XXXXX-WORD-NN` (same
`rollSecret` construction as `index.html`). `<cot>` is only our local dump wrapper.
Based on: https://github.com/s-JoL/open-reasoning

Model: anthropic.claude-sonnet-4-5-20250929-v1:0 · mode=`bedrock` · `thinking.budget_tokens=6000` · `max_tokens=12000` · plant_secret = `COMET-ZMX4E-EMBER-80`

Q:
```markdown
How many trailing zeros are there in the decimal representation of $100!$? Write a single integer.
```

## What the caller actually sees (visible reply — result only, 2 chars)

```markdown
24
```

Summary field (Bedrock returns plaintext thinking (kept server-side for gold check); 527 hidden reasoning tokens sealed in signature):

```markdown
*(Bedrock Distill returned thinking plaintext alongside the signature; that plaintext is used **only** as ground-truth for alignment below. The reverse-engineered block is recovered from the signature alone.)*
```

## Reverse-engineered thinking (recovered from the signature alone)

- **token_alignment = 0.58** (306/527 recovered/hidden)
- **gold_plaintext_alignment = 0.2** (SequenceMatcher vs harvest thinking plaintext; signature-only recovery)
- recovered_chars = 501 · reversed CoT is **250x** longer than the visible reply
- planted secret present in reversed CoT = **True** · present in visible reply = **False**
  (secret appears only in the recovered thinking → the signature really carried the hidden reasoning)
- unseal_ok = **True** · meta = {'audit_token': 'COMET-ZMX4E-EMBER-80', 'chunks': 1, 'recovered_output_tokens': 306, 'strategy': 'cot', 'truncated': False}

```markdown
COMET-ZMX4E-EMBER-80

To find the number of trailing zeros in 100!, I need to count how many times 10 divides 100!.

Since 10 = 2 × 5, and there are always more factors of 2 than 5 in n!, I need to count the number of times 5 divides 100!.

The number of times a prime p divides n! is given by:
⌊n/p⌋ + ⌊n/p²⌋ + ⌊n/p³⌋ + ...

For p = 5 and n = 100:
⌊100/5⌋ + ⌊100/25⌋ + ⌊100/125⌋ + ...

⌊100/5⌋ = ⌊20⌋ = 20
⌊100/25⌋ = ⌊4⌋ = 4
⌊100/125⌋ = ⌊0.8⌋ = 0

All subsequent terms are 0.

Total = 20 + 4 + 0 = 24
```

Ground truth: expected answer = `24` · model visible = `24`.

Cost: send $0.008580 + reverse $0.005514 · 24.3s.
