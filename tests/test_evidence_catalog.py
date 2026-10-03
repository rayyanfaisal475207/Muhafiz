import json
from unittest.mock import AsyncMock

import pytest

from src.evidence.catalog import (BindingProposal, SchemaProposal, convert_value, profile_records,
                                  propose_schema, serialize_record, values_at, validate_profile_compatibility)


def test_nested_relationship_pairing_preserves_array_identity():
    from src.evidence.catalog import matching_paths,shared_array_selectors
    assert shared_array_selectors('/readings/*/sensor','/readings/*/owner')=={'/readings/*'}
    assert matching_paths('/readings/*/sensor','/readings/0/sensor','/readings/*/owner','/readings/0/owner')
    assert not matching_paths('/readings/*/sensor','/readings/0/sensor','/readings/*/owner','/readings/1/owner')
    assert not shared_array_selectors('/sensors/*','/owners/*')


def test_profiles_discover_nested_unseen_fields_without_domain_mappings():
    profile = profile_records([{'sensor': {'lag': 1}, 'events': [{'reading': 2}]},
                               {'sensor': {'lag': 3}, 'events': [{'reading': None}]}])
    assert profile['fields']['/sensor/lag']['types'] == ['number']
    assert profile['fields']['/events/*/reading']['null'] == 1
    assert profile['schema_fingerprint'] == profile_records([
        {'sensor': {'lag': 100}, 'events': [{'reading': 40}]},
        {'sensor': {'lag': 20}, 'events': [{'reading': None}]}])['schema_fingerprint']


def test_nested_duplicate_keys_have_exact_separate_offsets():
    record = {'a': {'value': '30 بور'}, 'b': {'value': '30 بور'}, 'x/y': [{'value': 'Urdu'}]}
    text, offsets = serialize_record(record)
    assert json.loads(text) == record
    assert offsets['/a/value'] != offsets['/b/value']
    for pointer in ['/a/value', '/b/value', '/x~1y/0/value']:
        start,end = offsets[pointer]
        assert json.loads(text[start:end]) == values_at(record,pointer)[0][0]
    assert values_at(record,'/x~1y/*/value') == [('Urdu','/x~1y/0/value')]


@pytest.mark.parametrize('value', ['30 بور', '1 عدد + 6 گولیاں', True, float('inf')])
def test_numeric_transform_cannot_manufacture_quantities(value):
    with pytest.raises(ValueError): convert_value(value,'number')


def test_timezone_and_transform_are_explicit():
    with pytest.raises(ValueError): convert_value('2026-01-01','date')
    with pytest.raises(ValueError): convert_value('true','boolean')
    with pytest.raises(ValueError): convert_value(1,'eval')
    assert convert_value('2026-01-01T00:00:00Z','date')[1].tzinfo is not None


def test_schema_drift_requires_review_but_missing_values_do_not_change_meaning():
    reviewed = profile_records([{'lag':2,'record':'R1'}])
    validate_profile_compatibility([{'lag':None,'record':'R2'}],reviewed)
    validate_profile_compatibility([{'record':'R3'}],reviewed)
    for record in [{'lag':'2','record':'R4'},{'lag':True},{'new_field':2}]:
        with pytest.raises(ValueError,match='schema changed'):
            validate_profile_compatibility([record],reviewed)


@pytest.mark.asyncio
async def test_model_binding_without_documentation_is_rejected():
    model = AsyncMock()
    model.structured.return_value = SchemaProposal(entity_label='Sensor',entity_meaning='A sensor reading',
        grain='One reading',fields=[BindingProposal(pointer='/lag',label='Lag',meaning='Observed delay',
        grain='One reading',transform='number',unit='seconds',evidence_quote='invented documentation')])
    with pytest.raises(ValueError,match='support'):
        await propose_schema(model,[{'lag':2}],'Real documentation')
    assert model.structured.call_args.kwargs['allow_cloud'] is False
