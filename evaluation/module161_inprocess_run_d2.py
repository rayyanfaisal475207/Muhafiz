# -*- coding: utf-8 -*-
"""Module 161 — MEASUREMENT ARM ONLY. Runs the live runner with candidate
D2 from module161_description_candidates.json patched over the shipped
description IN PROCESS (nothing on disk changes), so the whole path —
router semantic override -> XAGG -> applicant_accused_overlap -> renderer ->
verifier -> answer — is exercised live once. D2 is NOT shipped: it names
"under investigation" and its P1 score (0.325) shows the gain is wording
overlap with Q2/L2, not a better description (MODULE161_RESULT.md §6.3).

    PYTHONPATH=. python -X utf8 evaluation/module161_inprocess_run_d2.py --tag after_d2_armed --runs 3 --ids Q2
"""
import json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "evaluation"))
from src.pipeline import semantic_dispatch as sd
cands = json.loads((ROOT / "docs/gold-qa-wave2-results/module161_description_candidates.json").read_text(encoding="utf-8"))["candidates"]
sd.CAPABILITY_DESCRIPTIONS["applicant_accused_overlap"] = cands["D2"]
import runpy
sys.argv[0] = str(ROOT / "evaluation" / "module161_inprocess_run.py")
runpy.run_path(sys.argv[0], run_name="__main__")
