# Phase 2.2 — Word tokenization: urduhack vs. Stanza vs. custom regex

**Decision: a custom regex tokenizer**, `tokenize()` in
[src/ingestion/tokenizer.py](../src/ingestion/tokenizer.py). It is the single shared
tokenizer BM25 (Phase 2.5) and NER (Phase 4.5) both consume.

Tested on the actual stack: **Python 3.13.1, Windows**. Both candidate libraries were
installed and run, not assessed from their READMEs.

---

## urduhack — rejected, does not work on Python 3.13

`pip install urduhack` appears to succeed, which is the trap. urduhack 1.1.0 requires
`tensorflow~=2.3.0`, and no TensorFlow wheel exists for cp313, so pip silently
backtracks to **urduhack 1.0.3** and installs it *without* TensorFlow. Import then
fails:

```
File "urduhack/tokenization/keras_tokenizer.py", line 12, in <module>
    import tensorflow as tf
ModuleNotFoundError: No module named 'tensorflow'
```

It also wanted to downgrade `click` 8.x → 7.1.2 in this environment, which would break
the uvicorn/FastAPI CLI stack.

This confirms the risk [ARCHITECTURE.md §4.2](ARCHITECTURE.md) flagged — the package
documents Python 3.6/3.7 support only. It is not a theoretical concern; it is a hard
failure.

**Knock-on effect:** urduhack's normalization utilities are unavailable too, so the Urdu
character normalization in [src/ingestion/text_normalizer.py](../src/ingestion/text_normalizer.py)
is hand-rolled against the same Unicode mappings rather than imported.

## Stanza — works, but rejected for this call site

stanza 1.14.0 installs cleanly on 3.13. `torch` 2.13.0+cpu was already present in this
environment, so it added no new heavy dependency. The Urdu UD model downloads in ~6s and
tokenization quality is good:

```
'پولیس نے ملزم کو گرفتار کر لیا۔'
  → ['پولیس', 'نے', 'ملزم', 'کو', 'گرفتار', 'کر', 'لیا', '۔']
```

It loses on **cost, at the one place this function is hottest.** `retrieve_bm25()` builds
its index in memory on every query, so the tokenizer runs across the whole candidate pool
per request. Measured on this machine:

| | per chunk | 500 chunks |
|---|---|---|
| Stanza (Urdu UD, CPU) | 26.7 ms | 13.4 s |
| custom regex | 0.08 ms | 0.04 s |

~330×. On top of that: a 3.7s cold model load, and a downloaded model artifact that
ingestion would then depend on. For output that on this corpus amounts to splitting on
whitespace and punctuation, that is not a trade worth making.

## Custom regex — chosen

The corpus is police/legal prose and form fields, not free web text. The tokens that
matter for lexical retrieval are Urdu words, English words, and identifiers
(`FIR-2026-ARMS-003`, CNICs, section numbers). Whitespace + punctuation segmentation
covers all three at a cost the per-query BM25 rebuild absorbs.

What it does beyond `.lower().split()`:

- Runs Urdu character normalization first, so `ک`/`ك`, `ی`/`ي`, `ہ`/`ه` and
  Arabic-Indic vs. ASCII digits produce the same term.
- Strips Urdu punctuation (`۔ ؟ ،`) that whitespace-splitting left glued to words —
  `ہے۔` and `ہے` were previously two different index terms.
- Treats ZWNJ as a word separator.
- Emits compound identifiers both whole and in parts, so `ARMS-003` and
  `FIR-2026-ARMS-003` both hit.

**Where it loses to Stanza:** it cannot segment Urdu compounds written without a space,
and cannot split orthographically fused clitics. Neither shows up as a retrieval failure
on this corpus, where Urdu is space-separated. Revisit if Phase 9's eval shows the Urdu
BM25 slice lagging the English one.

## Dependency outcome

**No new entry in `requirements.txt`.** Stanza is left installed locally but deliberately
unlisted: Phase 4.5's NER may want it, where the cost is per-document and amortized very
differently from per-query, and that is the right place to make that call.

## Related

Sentence splitting (Phase 2.1) is a separate, also-regex module,
[src/ingestion/sentence_splitter.py](../src/ingestion/sentence_splitter.py) — matching the
finalized model roster, which lists sentence splitting as "rule-based regex (no model)".
The model server's `/split_sentences` endpoint is itself a regex; reproducing it in-process
avoids making ingestion depend on a rotating free-tier ngrok tunnel.
