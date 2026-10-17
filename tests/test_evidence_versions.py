from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from src.evidence.runtime_registry import build_runtime
from src.evidence.types import Scope


@pytest.mark.asyncio
@pytest.mark.parametrize('version',['2.1'])
async def test_compatible_version_selection_preserves_scope_and_no_cloud_fallback(monkeypatch,version):
    monkeypatch.setenv('EVIDENCE_WORKFLOW_VERSION',version)
    scope=Scope(user_id=uuid4(),session_id=uuid4())
    store=type('Store',(),{})()
    store.catalog=AsyncMock(return_value={});store.resolve_mentions=AsyncMock(return_value=[])
    store.ground_forms=AsyncMock(return_value={});store.snapshot=AsyncMock(return_value='stable')
    # The invariant is NOT "the runtime never asks for the cloud". Reading the
    # question and planning may use the configured cloud model when
    # EVIDENCE_PLANNER_CLOUD says so -- that is a deliberate choice, made once,
    # in configuration. What must never happen is an AUTOMATIC fallback, and
    # the case passages themselves must never leave the machine.
    monkeypatch.delenv('EVIDENCE_PLANNER_CLOUD', raising=False)
    from src.evidence.model import cloud_planning_enabled
    assert cloud_planning_enabled() is False, 'cloud must be off unless switched on'

    asked_cloud_for=[]
    class Model:
        diagnostics=[]
        async def structured(self,contract,system,payload,**kwargs):
            if kwargs.get('allow_cloud'):
                asked_cloud_for.append(contract.__name__)
            if contract.__name__=='QuestionClass':
                return contract(shape='single',category='other',asks=1)
            if contract.__name__=='QueryUnderstanding':return contract(language='en')
            assert payload['conversation'] is None
            return contract(question='show records',standalone_question='show records',clarification='Which records?')
    answer=await build_runtime(store,Model()).run(uuid4(),'show records',scope)
    assert answer.status=='clarification' and answer.text=='Which records?'
    assert answer.health['workflow_version']==version
    # Only reading and planning may be cloud-eligible. Composition -- the only
    # stage handed the source passages -- must never be.
    # The line this invariant draws is WHAT THE STAGE IS SHOWN, not how many
    # stages there are. Classification, reading and planning are handed the
    # QUESTION and the catalog's semantics; composition is the only stage
    # handed the source passages themselves, and it is the one that must
    # never be cloud-eligible. Adding a stage that sees only the question
    # extends the first list without touching the property being protected.
    assert set(asked_cloud_for) <= {'QuestionClass','QueryUnderstanding','QuestionPlan'}, (
        'only question-level stages may be cloud-eligible, got %s' % asked_cloud_for)
    assert 'Draft' not in asked_cloud_for and 'Review' not in asked_cloud_for, (
        'composition sees the case passages and must never leave the machine')


def test_unknown_runtime_version_does_not_select_legacy_pipeline(monkeypatch):
    monkeypatch.setenv('EVIDENCE_WORKFLOW_VERSION','legacy')
    with pytest.raises(ValueError,match='no legacy fallback'):build_runtime(None)


def test_the_frozen_kernel_refuses_to_load_when_its_seal_cannot_be_verified(monkeypatch):
    """2.0 is sealed, and its sealed bytes no longer exist anywhere.

    This case used to assert that 2.0 BUILDS AND RUNS, and it had been
    failing. The reason is not a broken guard -- the guard is working.
    MEASURED, 16 October 2026: only one commit ever touched
    runtime_v2_0.py, and neither its committed form nor any line-ending
    variant of it hashes to the value in runtime_versions.json. The seal
    was taken from a working tree that was never committed, so the reviewed
    copy is gone and no one can verify what 2.0 is.

    Refusing is therefore the CORRECT behaviour, and this asserts it. The
    alternative -- re-sealing to whatever is on disk -- would make the
    manifest attest to a review that never happened, which defeats the only
    thing a seal is for. Every request runs 2.1.

    If 2.0 is ever wanted back, someone reviews runtime_v2_0.py as it now
    stands and re-seals deliberately; this test then changes with it.
    """
    monkeypatch.setenv('EVIDENCE_WORKFLOW_VERSION','2.0')
    with pytest.raises(RuntimeError,match='cannot be verified'):
        build_runtime(None)
