"""
Module 78 — live runner.

`scripts/module74_live_runs.py` verbatim (login / SSE parse / backend.log
slice / XAGG + data-half line capture), with the paraphrase probes this
module's acceptance bar needs added to its PROBES table before main() runs.
Importing it rather than copying it means both phases are measured by the
same code Modules 74 and 77 used.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import scripts.module74_live_runs as m  # noqa: E402

m.PROBES.update({
    # Module 77's own two paraphrases, verbatim — the controls that already
    # routed RAG BEFORE this change, re-run so the fix is shown not to have
    # broken them.
    "P-KB3": (
        "Under police law, is the person who records an FIR supposed to be a "
        "different officer from the one who investigates it — and what does "
        "our own data actually show about that?"
    ),
    "P-KB9": (
        "When a death looks suspicious the police must formally investigate "
        "the cause of death — does our system record that anywhere?"
    ),
    # Two NEW paraphrases this module has never measured, one per question,
    # in the same language as its gold text.
    "P2-KB3": (
        "Is the officer who writes up an FIR meant to be a different person "
        "from the one who investigates the case, and do our own case records "
        "bear that out across the whole caseload?"
    ),
    "P2-KB9": (
        "Agar maut mashkook lage to kya police par maut ki wajah maloom karne "
        "ki qanooni zimmedari hai, aur kya hamare record mein aisi tehqeeqat "
        "kahin darj hoti hai?"
    ),
})

if __name__ == "__main__":
    m.main()
