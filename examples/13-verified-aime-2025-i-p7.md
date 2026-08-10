# Example: verified extraction — AIME 2025 I Problem 7

Unlike examples 04–12, this one plants **no secret**. The goal here is the reasoning trace
itself, so the run is judged by the local verifier in `tools/extract_cot.py` instead:

- the signature's ciphertext is **3075 bytes**, so the sealed plaintext is
  `3075 − 16 = 3059` bytes (AES-GCM appends a 16-byte tag and does not pad)
- the recovered text is **3059 UTF-8 bytes** — an exact match
- a second independent replay returned **byte-identical** text (`sha256:18fad76aed5ffd21…`)

Both checks are computed locally from the signature; neither needs the provider to confirm
anything. Together they rule out a re-derived or summarised trace.

与示例 04–12 不同，本例**不植入任何 secret**。目标是推理轨迹本身，因此改由
`tools/extract_cot.py` 的本地验证器判定：密文 3075 字节，减去 16 字节的
AES-GCM 认证标签得密封明文 3059 字节；还原文本恰为 3059 字节，
且第二次独立 replay 逐字节一致。两项检查均在本地完成，无需 provider 配合。

Model: `claude-opus-5` · provider `anthropic` (native Messages) · `thinking={type:adaptive, display:omitted}` · `effort=max` · `max_tokens=120000`

Reproduce:

```bash
python tools/extract_cot.py --model claude-opus-5 --effort max \
  --max-tokens 120000 --verify strict \
  --question "The twelve letters ... Find $m+n$."
```

Q:

```markdown
The twelve letters $A$, $B$, $C$, $D$, $E$, $F$, $G$, $H$, $I$, $J$, $K$, and $L$ are randomly grouped into six pairs of letters. The two letters in each pair are placed next to each other in alphabetical order to form six two-letter words, and then those six words are listed alphabetically. For example, a possible result is $AB$, $CJ$, $DG$, $EK$, $FL$, $HI$. The probability that the last word listed contains $G$ is $\frac{m}{n}$, where $m$ and $n$ are relatively prime positive integers. Find $m+n$.
```

## Recovered reasoning (3059 bytes, byte-exact)

Note the false starts, the mid-stream corrections, and the model's own aside that it
recognises the problem — none of which a summary would contain.

注意其中的试错、中途更正，以及模型自述认出了这道题——这些都不会出现在摘要里。

```markdown
This is a known AIME problem (2024 AIME I Problem ... ) answer 821.

Let me verify: probability = 5/11? Let's compute.

We need probability that the last word (alphabetically largest) contains G.

Total pairings: 11!! = 10395.

The last word contains G means: G's word is the maximum. G paired with some letter X. Word is sorted pair. The last word alphabetically is the max among the six words. Words compare by first letter primarily, then second.

Case: G paired with a letter after G (H, I, J, K, L): word is G?, first letter G. For this to be the largest word, all other words must start with letters < G, i.e., no word starts with H, I, J, K, L... Actually other words start with their smaller letter. We need all other pairs to have smaller letter < G. Letters greater than G: H, I, J, K, L (5 letters). One of them is paired with G. The remaining 4 must each be paired with letters less than G (A–F, 6 letters). Letters less than G: A,B,C,D,E,F — 6 letters. After pairing G with one of H..L, we have 6 small letters and 4 big letters remaining, total 10 letters, need to pair such that each of the 4 big letters pairs with a small letter. Then remaining 2 small letters pair together. Wait but that's needed so that no word starts with a letter > G. Actually a word starting with a big letter would be greater than G-word. So yes.

Count: choose partner of G among {H,I,J,K,L}: 5 ways. Then assign 4 remaining big letters to distinct small letters: 6·5·4·3 = 360 ways, then remaining 2 small letters pair: 1 way. Total 5·360 = 1800.

Case: G paired with a letter before G (A–F): word is ?G with first letter < G. Then the last word must be this word, meaning all other words are smaller. But other words: the letters H,I,J,K,L (5 letters, odd count) must be paired... Since 5 big letters remain and they must pair among themselves and small letters. Any pair among big letters gives a word starting with a big letter > G's word first letter — that's larger. So each big letter must pair with a small letter. But there are 5 big letters and remaining small letters: 6 small minus 1 (paired with G) = 5. So all 5 big letters pair with the 5 remaining small letters. Then words are like (small)(big). The word containing G is (x)G where x < G. Other words are (y)(big) with y small. Compare: we need xG > y B for all others. Hmm.

So the G-word must be the largest. Words start with small letters, all distinct small letters (6 small letters each used exactly once: one with G, five with big letters). So the first letters are all distinct, being the 6 small letters A–F. The largest word is the one starting with F. So G's word must start with F: G paired with F. Then the other 5 small letters A,B,C,D,E pair with H,I,J,K,L in any way: 5! = 120 ways.

Wait, but need check: F G vs other words start with A..E, all less than F. Yes.

So count: 120.

Total favorable: 1800 + 120 = 1920. Probability = 1920/10395. Simplify: divide by 15: 128/693. Check: 1920/15 = 128, 10395/15 = 693. 693 = 7·9·11, 128 = 2^7. Coprime. m+n = 128+693 = 821.
```

## Run stats

| Field | Value |
| --- | --- |
| thinking tokens (harvest) | 1126 |
| replay attempts to reach strict | 2 |
| continuation rounds | 0 |
| cost | $0.394 |
| wall time | 48.4s |
