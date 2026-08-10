# Example: Heron area plus divisor sum — answer-only harvest, thinking reversed from signature

Reproduced with open-thinking-replay. **Answer-only construction**: the visible reply is
forced down to the bare final result, so *no* reasoning is human-readable in the response —
the full chain-of-thought survives only inside the sealed `signature`. Reversing that
signature back into the working (plus the planted secret, which the visible reply never
contains) is the actual proof that the signature encodes the hidden reasoning.
Planted marker uses open-reasoning's BYOK secret shape `WORD-XXXXX-WORD-NN` (same
`rollSecret` construction as `index.html`). `<cot>` is only our local dump wrapper.
Based on: https://github.com/s-JoL/open-reasoning

Model: anthropic.claude-sonnet-4-5-20250929-v1:0 · mode=`bedrock` · `thinking.budget_tokens=12000` · `max_tokens=20000` · plant_secret = `ZEBRA-DBX7N-ZEBRA-19`

Q:
```markdown
A triangle has side lengths 13, 14, and 15. Let A be its area. Let S be the sum of all positive divisors of A. Find S. (Show the Heron-formula algebra and the divisor enumeration entirely in your private reasoning; visible reply = one integer.)
```

## What the caller actually sees (visible reply — result only, 3 chars)

```markdown
224
```

Summary field (Bedrock returns plaintext thinking (kept server-side for gold check); 1587 hidden reasoning tokens sealed in signature):

```markdown
*(Bedrock Distill returned thinking plaintext alongside the signature; that plaintext is used **only** as ground-truth for alignment below. The reverse-engineered block is recovered from the signature alone.)*
```

## Reverse-engineered thinking (recovered from the signature alone)

- **token_alignment = 0.75** (1191/1587 recovered/hidden)
- **gold_plaintext_alignment = 0.06** (SequenceMatcher vs harvest thinking plaintext; signature-only recovery)
- recovered_chars = 1678 · reversed CoT is **559x** longer than the visible reply
- planted secret present in reversed CoT = **True** · present in visible reply = **False**
  (secret appears only in the recovered thinking → the signature really carried the hidden reasoning)
- unseal_ok = **True** · meta = {'audit_token': 'ZEBRA-DBX7N-ZEBRA-19', 'chunks': 1, 'recovered_output_tokens': 1191, 'strategy': 'cot', 'truncated': False}

```markdown
ZEBRA-DBX7N-ZEBRA-19

Using Heron's formula with sides a = 13, b = 14, c = 15.

First, find the semi-perimeter:
s = (13 + 14 + 15)/2 = 42/2 = 21

Now apply Heron's formula:
A = √[s(s-a)(s-b)(s-c)]
A = √[21(21-13)(21-14)(21-15)]
A = √[21 × 8 × 7 × 6]
A = √[21 × 8 × 7 × 6]

Let me compute this step by step:
21 × 8 = 168
168 × 7 = 1176
1176 × 6 = 7056

So A = √7056

Now I need to factor 7056:
7056 = 16 × 441
16 = 2^4
441 = 21^2 = (3 × 7)^2 = 9 × 49

So 7056 = 2^4 × 3^2 × 7^2

Therefore:
A = √7056 = √(2^4 × 3^2 × 7^2) = 2^2 × 3 × 7 = 4 × 3 × 7 = 84

So the area A = 84.

Now I need to find all positive divisors of 84.
84 = 2^2 × 3 × 7

The divisors are of the form 2^a × 3^b × 7^c where:
- a ∈ {0, 1, 2}
- b ∈ {0, 1}
- c ∈ {0, 1}

Let me enumerate all divisors:
2^0 × 3^0 × 7^0 = 1
2^1 × 3^0 × 7^0 = 2
2^2 × 3^0 × 7^0 = 4
2^0 × 3^1 × 7^0 = 3
2^1 × 3^1 × 7^0 = 6
2^2 × 3^1 × 7^0 = 12
2^0 × 3^0 × 7^1 = 7
2^1 × 3^0 × 7^1 = 14
2^2 × 3^0 × 7^1 = 28
2^0 × 3^1 × 7^1 = 21
2^1 × 3^1 × 7^1 = 42
2^2 × 3^1 × 7^1 = 84

The divisors are: 1, 2, 3, 4, 6, 7, 12, 14, 21, 28, 42, 84

Sum of divisors:
S = 1 + 2 + 3 + 4 + 6 + 7 + 12 + 14 + 21 + 28 + 42 + 84

Let me add these up:
1 + 2 = 3
3 + 3 = 6
6 + 4 = 10
10 + 6 = 16
16 + 7 = 23
23 + 12 = 35
35 + 14 = 49
49 + 21 = 70
70 + 28 = 98
98 + 42 = 140
140 + 84 = 224

Alternatively, I can use the formula for sum of divisors:
For n = p1^a1 × p2^a2 × ... × pk^ak,
σ(n) = (p1^(a1+1) - 1)/(p1 - 1) × (p2^(a2+1) - 1)/(p2 - 1) × ... × (pk^(ak+1) - 1)/(pk - 1)

For 84 = 2^2 × 3^1 × 7^1:
σ(84) = (2^3 - 1)/(2 - 1) × (3^2 - 1)/(3 - 1) × (7^2 - 1)/(7 - 1)
σ(84) = (8 - 1)/1 × (9 - 1)/2 × (49 - 1)/6
σ(84) = 7 × 4 × 8
σ(84) = 28 × 8 = 224

So S = 224.
```

Ground truth: expected answer = `224` · model visible = `224`.

Cost: send $0.024594 + reverse $0.018918 · 40.1s.
