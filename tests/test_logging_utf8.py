"""Log records carrying Urdu must survive a non-UTF-8 stdout.

[Found live 2026-09-08 while verifying Module 36.] `uvicorn ... > backend.log`
on Windows gives a stdout encoded with the console ANSI codepage (cp1252), not
UTF-8. Every log record containing Urdu then raised `UnicodeEncodeError` inside
`logging`'s `emit()`, and — because `logging` swallows that — the record was
never written at all.

That silently deleted the `XAGG <kind>: ...` diagnostics, which are the only
way to tell which aggregate ran (the SSE stream reports only `route='XAGG'`).
It failed preferentially on Urdu-carrying aggregates, i.e. exactly the ones the
Gold-QA modules are verified against.

These tests pin the fix at the handler level. `test_cp1252_stream_loses_the_record`
documents the original defect against a raw handler, so the regression is
described by a test rather than only by a comment.
"""

import io
import logging

import pytest

from src.main import _utf8_log_stream

# A real value from the weapon register, and the shape of Module 35's own
# diagnostic line. Kept as escapes so this file stays pure ASCII on disk --
# a .py read under the system ANSI codepage is the sibling of the bug itself.
URDU_ARREST_STATUS = "گرفتار"  # گرفتار
URDU_UNLICENSED = "بغیر لائسنس"  # بغیر لائسنس


def _cp1252_stream():
    """Emulate a redirected Windows console: a text stream over bytes, cp1252."""
    raw = io.BytesIO()
    # write_through so bytes land without an explicit flush, matching a
    # line-buffered console closely enough for this assertion.
    return raw, io.TextIOWrapper(raw, encoding="cp1252", write_through=True)


def _emit(stream, message):
    """Send one record through an isolated logger and return it."""
    logger = logging.getLogger(f"utf8test.{id(stream)}")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    try:
        logger.info(message)
    finally:
        handler.flush()
        logger.handlers.clear()


def test_cp1252_stream_loses_the_record():
    """The original defect, pinned: a raw handler over cp1252 writes NOTHING.

    Not an aspiration -- this is what production did. If a future change makes
    a bare cp1252 stream survive Urdu, this test failing is good news and
    should simply be retired.
    """
    raw, stream = _cp1252_stream()
    _emit(stream, f"XAGG weapon_compliance: 30 of 32 {URDU_UNLICENSED}")
    assert raw.getvalue() == b"", (
        "expected the cp1252 stream to drop the record entirely -- if this now "
        "writes, the platform behaviour changed and this test can be retired"
    )


def test_utf8_log_stream_preserves_urdu():
    """After `_utf8_log_stream`, the same record is written and readable."""
    raw, stream = _cp1252_stream()
    _emit(_utf8_log_stream(stream), f"XAGG arrest_rate: 11 FIR(s) {URDU_ARREST_STATUS}")

    written = raw.getvalue().decode("utf-8")
    assert URDU_ARREST_STATUS in written
    assert "XAGG arrest_rate: 11 FIR(s)" in written


def test_ascii_records_are_unaffected():
    """The overwhelmingly common case must be byte-identical."""
    raw, stream = _cp1252_stream()
    _emit(_utf8_log_stream(stream), "Muhafiz starting up...")
    assert raw.getvalue().decode("utf-8").strip() == "Muhafiz starting up..."


def test_a_stream_that_cannot_be_retargeted_still_logs():
    """A stream with neither `reconfigure` nor `.buffer` must not crash startup.

    Logging is never worth failing the application over, so the helper returns
    the stream unchanged rather than raising.
    """

    class _Bare(io.StringIO):
        def reconfigure(self, **kwargs):  # noqa: D401 - emulate refusal
            raise ValueError("cannot reconfigure")

    bare = _Bare()
    assert _utf8_log_stream(bare) is bare
    _emit(bare, "plain ascii still logs")
    assert "plain ascii still logs" in bare.getvalue()


@pytest.mark.parametrize("text", [URDU_ARREST_STATUS, URDU_UNLICENSED, "mixed 30 " + URDU_UNLICENSED])
def test_urdu_variants_round_trip(text):
    raw, stream = _cp1252_stream()
    _emit(_utf8_log_stream(stream), text)
    assert text in raw.getvalue().decode("utf-8")
