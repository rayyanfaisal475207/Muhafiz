"""
Module 64 — offline probe: why KB6's own wording does not retrieve the chunk
that holds its gold answer, while paraphrases of the same question do.

NO BACKEND IS NEEDED. This talks to Chroma and the embeddings endpoint
directly, plus the same LLM helpers `rag.py` calls to build its query
variants. Every number in `docs/gold-qa-wave2-results/MODULE64_RESULT.md`
comes from one of the four modes below.

    PYTHONPATH=. python -X utf8 scripts/module64_probe.py target
    PYTHONPATH=. python -X utf8 scripts/module64_probe.py mechanism
    PYTHONPATH=. python -X utf8 scripts/module64_probe.py variants [trials]
    PYTHONPATH=. python -X utf8 scripts/module64_probe.py regress [trials]

`target`     — locates, by chunk id and verbatim text, the chunk(s) that
               actually carry gold's guideline half. The tracker calls them
               `c19`/`c18`; this establishes that those labels still denote
               what the tracker thinks they do before anything is built on
               them.
`mechanism`  — the ranked retrievals that decide the diagnosis: gold's own
               wording, its English rendering, Module 39's English
               paraphrase, and the four controls that separate LANGUAGE,
               COMPOUNDNESS and VOCABULARY as candidate mechanisms.
`variants`   — the FULL live variant set (question + 2 expansions +
               cross-script variant + 2 statute hypotheses + the English
               rendering), merged exactly as `_retrieve_candidates()` merges
               it, so the merged rank can be compared against the
               `TOP_K_RETRIEVAL * CROSS_CASE_RETRIEVAL_MULTIPLIER` cut.
`regress`    — the statute-hypothesis pool for all eight gold KB questions
               and three non-gold paraphrases, against each question's own
               target chunk. Run it once with the prompt stashed and once
               with it applied; that is the §7 regression guard at the layer
               this module actually changes.

CHROMA IS A PRIVATE COPY. `CHROMA_PERSIST_DIR` in the repo `.env` is the
RELATIVE `./data/chroma_db`, which resolves against the process's cwd — the
standing trap on this programme. This script resolves it to an absolute path
under the worktree it is run from and never touches the shared store.
"""

import asyncio
import json
import os
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_CHROMA = (_ROOT / "data" / "chroma_db").resolve()

try:  # pragma: no cover - convenience only
    from dotenv import load_dotenv

    load_dotenv(_ROOT / ".env", override=False)
except Exception:
    pass
os.environ["CHROMA_PERSIST_DIR"] = str(_CHROMA)

from src import config  # noqa: E402

config.CHROMA_PERSIST_DIR = _CHROMA

from src.pipeline.cross_script_variant import generate_cross_script_variant  # noqa: E402
from src.pipeline.query_expander import expand_query  # noqa: E402
from src.pipeline.statute_hypothesis import (  # noqa: E402
    generate_statute_queries,
    render_question_in_english,
)
from src.retrieval.embedder import embed_text  # noqa: E402
from src.retrieval.vector_store import query_similar  # noqa: E402

# The KB-only scope `_build_where()` produces for a legal-KB question.
WHERE = {"is_global": True}
# One variant's Chroma fetch on that scope: TOP_K_RETRIEVAL(10) *
# CROSS_CASE_RETRIEVAL_MULTIPLIER(3). The merged pool is cut to the same 30.
FETCH_TOP_K = config.TOP_K_RETRIEVAL * config.CROSS_CASE_RETRIEVAL_MULTIPLIER

C19 = "5_Forensics_guidelines_pdf_62ee00b3_c19"
C18 = "5_Forensics_guidelines_pdf_62ee00b3_c18"
C43 = "5_Forensics_guidelines_pdf_62ee00b3_c43"

KB6 = (
    "Kya forensics guidelines mein is bare mein kuch makhsoos likha hai ke "
    "baramad shuda aslaha darj hone se pehle kaise handle kiya jaye, aur kya "
    "hamara weapon register yeh darj karta hai ke us par amal hua ya nahi?"
)

# The four controls. Each isolates ONE candidate mechanism, and three of the
# four are negative results — which is the point: they are what rules the
# tracker's leading hypothesis out.
MECHANISM_QUERIES = {
    # 0. gold, and the two renderings that are already on the record.
    "gold_roman_urdu": KB6,
    "module52_english_rendering": (
        "Are there any specific provisions in the forensics guidelines regarding "
        "how recovered weapons should be handled before they are registered, and "
        "does our weapon register record whether action has been taken on them?"
    ),
    "module39_english_paraphrase": (
        "Do the forensics guidelines set any rule for how a recovered firearm "
        "must be packed before it is entered in the record, and does our own "
        "weapon register show whether that was done?"
    ),
    # 1. LANGUAGE? Gold's own question in Urdu script, and a Roman-Urdu
    #    question that keeps the language but changes the verb.
    "control_language__gold_in_urdu_script": (
        "کیا فارنسک گائیڈ لائنز میں اس بارے میں کچھ مخصوص لکھا ہے کہ برآمد شدہ "
        "اسلحہ درج ہونے سے پہلے کیسے ہینڈل کیا جائے، اور کیا ہمارا ویپن رجسٹر "
        "یہ درج کرتا ہے کہ اس پر عمل ہوا یا نہیں؟"
    ),
    "control_language__roman_urdu_saying_pack": (
        "Kya forensics guidelines mein koi qaida hai ke baramad shuda firearm ko "
        "record mein darj karne se pehle kis tarah pack kiya jaye, aur kya hamara "
        "weapon register batata hai ke aisa kiya gaya?"
    ),
    # 2. COMPOUNDNESS? Gold's norm clause with the data clause deleted, in
    #    gold's own vocabulary; and the data clause alone.
    "control_compound__norm_clause_only": (
        "Kya forensics guidelines mein is bare mein kuch makhsoos likha hai ke "
        "baramad shuda aslaha darj hone se pehle kaise handle kiya jaye?"
    ),
    "control_compound__data_clause_only": (
        "Kya hamara weapon register yeh darj karta hai ke us par amal hua ya nahi?"
    ),
    # 3. VOCABULARY? The same question in English, once with gold's verb and
    #    once with the guidelines' own verb.
    "control_vocab__english_with_golds_verb": (
        "Is anything specific written in the forensics guidelines about how "
        "recovered weapons should be handled before being recorded, and does our "
        "weapon register record whether this was complied with or not?"
    ),
    "control_vocab__english_with_the_corpus_verb": (
        "How must a recovered firearm be packaged as evidence?"
    ),
}

# The three non-gold paraphrases of §6, plus the eight gold KB questions, each
# against the chunk its own gold answer rests on. Written before they were run.
PARAPHRASES = {
    "P1_en": (
        "Before a seized pistol is logged into our records, do the forensic "
        "guidelines lay down anything about the state it has to be in, and would "
        "our weapons register even show whether that was followed?"
    ),
    "P2_ru": (
        "Jab police koi pistol qabza mein leti hai, to kya forensic guidelines "
        "mein koi hidayat hai ke usay record mein laane se pehle kis halat mein "
        "rakha jaye, aur kya hamara weapon register yeh dikhata hai ke aisa hua "
        "ya nahi?"
    ),
    # A DIFFERENT exhibit type in the SAME document — the generality control
    # for rule 4d. If the rule only worked for firearms it would be a
    # gold-specific fix wearing a general rule's clothes.
    "P3_en_other_exhibit": (
        "What do the forensic guidelines require for a blood sample taken in a "
        "case before it goes to the laboratory?"
    ),
}

REGRESS_TARGETS = {
    "KB1": "1_1898_Code_of_Criminal_Procedure_(Pakistan)_pdf_f9908363_c642",
    "KB2": "2_qanun-e-shahadat-order-1984_pdf_3e604153_c176",
    "KB3": "3_1527157863PoliceOrder2002_pdf_bdaa97d4_c112",
    "KB4": "4_Punjab-Police-Rules-III_pdf_68bb5d0d_c2187",
    "KB5": (
        "7_Anti-Rape (lnvestigation and Trial) Act_ 2021 & Rules, 2022 "
        "(Amendments upto date)_pdf_d10a4de5_c148"
    ),
    "KB6": C19,
    "KB8": "1_1898_Code_of_Criminal_Procedure_(Pakistan)_pdf_f9908363_c758",
    "KB9": "1_1898_Code_of_Criminal_Procedure_(Pakistan)_pdf_f9908363_c766",
    "P1_en": C19,
    "P2_ru": C19,
    "P3_en_other_exhibit": C43,
}


def _gold_questions() -> dict:
    rows = json.loads(
        (_ROOT / "evaluation" / "Gold_QA_Dataset_Final32_With_Answers.json").read_text(
            encoding="utf-8"
        )
    )
    return {r["id"]: r["question"] for r in rows}


def _rank(results: list, chunk_id: str):
    return next((i + 1 for i, c in enumerate(results) if c["id"] == chunk_id), None)


async def _ranked(query: str, top_k: int = FETCH_TOP_K) -> list:
    return await query_similar(query, await embed_text(query), top_k=top_k, where=WHERE)


# ── mode: target ──────────────────────────────────────────────────────────────


def mode_target() -> None:
    """Establish, by id and verbatim text, what carries gold's guideline half."""
    import chromadb

    client = chromadb.PersistentClient(path=str(_CHROMA))
    for name in ("muhafiz_kb", "muhafiz_community_reports", "muhafiz_entity_descriptions"):
        count = client.get_collection(name).count()
        print(f"{name}: {count}")
        if name == "muhafiz_entity_descriptions" and count == 0:
            raise SystemExit(
                "muhafiz_entity_descriptions is EMPTY — someone ran a full `pytest -q`, "
                "which empties it. Refusing to measure against an emptied store."
            )

    col = client.get_collection("muhafiz_kb")
    got = col.get(include=["documents", "metadatas"])
    needles = (
        "safety on",
        "live round",
        "packaged separately",
        "chain of custody",
        "documented, labeled, marked, photographed",
    )
    for cid, doc in zip(got["ids"], got["documents"]):
        low = (doc or "").lower()
        hit = [n for n in needles if n in low]
        if hit and "Forensics" in cid:
            print("=" * 72)
            print(cid, hit)
            print(doc)


# ── mode: mechanism ───────────────────────────────────────────────────────────


async def mode_mechanism() -> None:
    """Side-by-side ranked retrievals. This is the table in §1 of the result."""
    for name, q in MECHANISM_QUERIES.items():
        res = await _ranked(q)
        print("=" * 72)
        print(f"[{name}] c19_rank={_rank(res, C19)} c18_rank={_rank(res, C18)}")
        print("  q:", q[:200])
        for i, c in enumerate(res[:12], 1):
            print(f"   {i:2d}. {c.get('rrf_score', 0.0):.4f}  {c['id']}")


# ── mode: variants ────────────────────────────────────────────────────────────


async def mode_variants(trials: int = 3) -> None:
    """The full live variant set, merged the way `_retrieve_candidates()` does."""
    for t in range(trials):
        expansions = await expand_query(KB6, n=2)
        cross_script = await generate_cross_script_variant(KB6)
        hypotheses = await generate_statute_queries(KB6) or []
        english = await render_question_in_english(KB6)
        variants = (
            [KB6]
            + list(expansions)
            + ([cross_script] if cross_script else [])
            + list(hypotheses)
            + ([english] if english and english != KB6 else [])
        )
        print("=" * 72)
        print(f"TRIAL {t}")
        pool: dict[str, float] = {}
        for v in variants:
            res = await _ranked(v)
            print(f"  [c19 rank {str(_rank(res, C19)):>4}]  {v[:150]}")
            for c in res:
                pool[c["id"]] = max(pool.get(c["id"], 0.0), c.get("rrf_score", 0.0))
        merged = [c for c, _ in sorted(pool.items(), key=lambda kv: -kv[1])]
        rank = merged.index(C19) + 1 if C19 in merged else None
        print(
            f"  MERGED pool={len(merged)}  c19 merged-rank={rank}  "
            f"(cut to {FETCH_TOP_K}; above the cut = reaches the evaluator)"
        )


# ── mode: regress ─────────────────────────────────────────────────────────────


async def mode_regress(trials: int = 3) -> None:
    """The statute-hypothesis pool for all 8 KB questions + the 3 paraphrases."""
    gold = _gold_questions()
    questions = {k: gold[k] for k in ("KB1", "KB2", "KB3", "KB4", "KB5", "KB6", "KB8", "KB9")}
    questions.update(PARAPHRASES)
    for qid, q in questions.items():
        target = REGRESS_TARGETS[qid]
        for t in range(trials):
            hypotheses = await generate_statute_queries(q) or []
            pool: dict[str, float] = {}
            for h in hypotheses:
                for c in await _ranked(h):
                    pool[c["id"]] = max(pool.get(c["id"], 0.0), c.get("rrf_score", 0.0))
            merged = [c for c, _ in sorted(pool.items(), key=lambda kv: -kv[1])][:FETCH_TOP_K]
            rank = merged.index(target) + 1 if target in merged else None
            print(f"{qid} t{t}: target_rank_in_hypothesis_pool={rank}")
            for h in hypotheses:
                print("      ", h[:180])


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "mechanism"
    trials = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    if mode == "target":
        mode_target()
    elif mode == "mechanism":
        asyncio.run(mode_mechanism())
    elif mode == "variants":
        asyncio.run(mode_variants(trials))
    elif mode == "regress":
        asyncio.run(mode_regress(trials))
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main()
