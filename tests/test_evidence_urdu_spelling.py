"""One name typed two ways is one name.

MEASURED, and the reason this file exists. The records spell a suspect's
name ذیشان. Urdu typists routinely write زیشان -- ذ and ز are both "z" --
and the two look near-identical on screen:

    resolve_mentions("زیشان کس کس مقدمے میں ملزم ہے؟")  ->  0 matches, 203s, unavailable
    resolve_mentions("ذیشان کس کس مقدمے میں ملزم ہے؟")  -> 10 matches, 35s, answered
    resolve_mentions("zeeshan")                        -> 11 matches, answered

So the ENGLISH spelling worked and the commoner Urdu one did not. Two
separate defects caused it, and both are pinned here because both were
invisible:

1. `ScopedStore.resolve` devowelled the query term AS TYPED and compared it
   against the ROMANISED skeleton of each stored name -- Urdu against Latin,
   which is always false. The term is now skeletonised too.

2. `roman_skeleton` (migration 058) knew only the URDU letter forms, not the
   Arabic ones Urdu replaced (ك/ک, ه/ہ, ي/ی), which arrive constantly from
   Arabic keyboard layouts and pasted text. Its letter map was also one
   character short, so ة was dropped and إ took ة's target -- and anything
   appended to that map landed one position out.

These are unit tests over the skeleton function's own contract. The
behaviour they protect is cross-script CANDIDATE generation: a shared
skeleton is a reason to offer a name, never to merge two identities.
"""
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from dotenv import dotenv_values

# Urdu letters, by code point, so an editor or a font cannot make two
# different characters look like the same test.
KEHEH_URDU = "ک"        # ک  U+06A9
KAF_ARABIC = "ك"        # ك  U+0643
HEH_GOAL_URDU = "ہ"     # ہ  U+06C1
HEH_ARABIC = "ه"        # ه  U+0647
YEH_URDU = "ی"          # ی  U+06CC
YEH_ARABIC = "ي"        # ي  U+064A

ZEESHAN_DHAL = "ذیشان"       # ذیشان, as recorded
ZEESHAN_ZAY = "زیشان"        # زیشان, as often typed
KASHIF_URDU = KEHEH_URDU + "اشف"       # کاشف
KASHIF_ARABIC = KAF_ARABIC + "اشف"     # كاشف


@pytest_asyncio.fixture
async def test_db():
    """A connection to the ISOLATED evidence test database, never production.

    The guard is the same one `test_evidence_postgres.py` applies: the DSN
    must name a database inside the test namespace or the fixture refuses to
    connect.
    """
    cfg = dotenv_values(Path(__file__).resolve().parents[1] / ".env.evidence-test")
    dsn = cfg.get("EVIDENCE_QUERY_DATABASE_URL", "")
    assert "/muhafiz_evidence_test_" in dsn, "refusing a non-test database"
    conn = await asyncpg.connect(dsn)
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
def skeleton(test_db):
    """roman_skeleton as the isolated test database actually defines it.

    Read from the database rather than reimplemented in Python: the function
    lives in a migration, and a Python copy of it would pass while the
    deployed one stayed broken -- which is precisely the failure being
    pinned.
    """
    async def call(text):
        return await test_db.fetchval("SELECT roman_skeleton($1)", text)
    return call


@pytest.mark.asyncio
@pytest.mark.parametrize("one,two,why", [
    (ZEESHAN_DHAL, ZEESHAN_ZAY, "ذ and ز are both z -- the measured failure"),
    (KASHIF_URDU, KASHIF_ARABIC, "Urdu keheh vs Arabic kaf"),
    ("سرفراز",
     "صرفراز", "س and ص are both s"),
    ("احمد", "ا" + HEH_ARABIC + "مد",
     "ح and Arabic ه both reduce to h"),
    ("فیصل", "ف" + YEH_ARABIC + "صل",
     "Urdu yeh vs Arabic yeh"),
])
async def test_the_same_name_typed_two_ways_has_one_skeleton(skeleton, one, two, why):
    first, second = await skeleton(one), await skeleton(two)
    assert first == second, f"{why}: {one} -> {first!r}, {two} -> {second!r}"
    assert first, "a name must not skeletonise to nothing"


@pytest.mark.asyncio
async def test_a_roman_query_passes_through_unchanged(skeleton):
    """Every cross-script match that works today goes through Latin input.

    The function is applied to the QUERY as well as the stored label now, so
    if it rewrote Latin text, every working English-to-Urdu match would
    break at once.
    """
    for word in ("zeeshan", "kashif", "ahmed", "sarfraz"):
        assert await skeleton(word) == word


@pytest.mark.asyncio
async def test_the_letter_map_lines_up(skeleton):
    """`translate()` DELETES a source character with no target.

    Migration 058 shipped 38 source letters against 37 targets, so ة was
    dropped and إ silently took ة's intended value. The symptom only
    appeared when a later migration appended letters and they all landed one
    position out -- ك mapped to 'h' and كاشف skeletonised to 'hashf'.
    """
    assert await skeleton(KASHIF_URDU) == "kashf"
    assert await skeleton(KASHIF_ARABIC) == "kashf"
    # ة reaches the map rather than falling off the end of it.
    assert await skeleton("ة") == "h"


@pytest.mark.asyncio
async def test_different_names_do_not_collide(skeleton):
    """A skeleton loose enough to merge two people would be worse than none.

    Collapsing ذ/ز and ك/ک is only safe while names that are genuinely
    different still separate. These matches are labelled cross_script and
    offered as candidates, so a collision here would manufacture an identity
    the records do not support.
    """
    distinct = [KASHIF_URDU, ZEESHAN_DHAL, "احمد",
                "بلال", "طارق"]
    skeletons = [await skeleton(name) for name in distinct]
    assert len(set(skeletons)) == len(distinct), (
        f"two different names share a skeleton: {list(zip(distinct, skeletons))}")


@pytest.mark.asyncio
async def test_diacritics_and_punctuation_are_ignored(skeleton):
    """A typist's zabar or a stray space must not change a name's identity."""
    plain = await skeleton(KASHIF_URDU)
    assert await skeleton(KASHIF_URDU + "َ") == plain      # with zabar
    assert await skeleton(" " + KASHIF_URDU + " ") == plain
    assert await skeleton(KASHIF_URDU + "،") == plain      # Urdu comma
