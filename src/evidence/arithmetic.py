"""Checked derived cells. Models select operands; Python performs the arithmetic."""
from decimal import Decimal, InvalidOperation

from .compiler import InvalidPlan
from .types import Claim, Coverage, EvidenceResult


def derive(operation, results, scope):
    from .answer import validate_claim
    if operation.operation not in {'difference','ratio'} or len(operation.operands) != 2:
        raise InvalidPlan('Invalid derived operation')
    selected, values, references = [], [], []
    for cell in operation.operands:
        result = next((r for r in results if r.output_id == cell.output_id),None)
        if result is None or result.derivation is not None:
            raise InvalidPlan('Derived cells require direct checked measurements')
        if result.operation.operation not in {'count_entities','count_assertions','sum','average','average_duration'}:
            raise InvalidPlan('Arithmetic operands must be measurements')
        if cell.field != 'value': raise InvalidPlan('Arithmetic requires the measured value cell')
        basis = 'observed_subset' if result.metric and result.metric.basis == 'observed_subset' else 'exact_population'
        claim = Claim(result_id=result.result_id,output_id=result.output_id,row_index=cell.row_index,
                      field=cell.field,population_basis=basis)
        try:
            value = validate_claim(claim,results,scope)
            if isinstance(value,bool): raise ValueError('Boolean is not a measurement')
            value = Decimal(str(value))
            if not value.is_finite(): raise ValueError('Non-finite measurement')
        except (ValueError,InvalidOperation) as exc:
            raise InvalidPlan('An arithmetic operand is unsupported') from exc
        selected.append(result); values.append(value); references.append(claim.model_dump(mode='json'))
    left,right = selected
    signature = lambda r: (r.operation.operation,r.operation.subject_type,r.operation.predicate_id,
                            r.operation.start_predicate_id,r.operation.end_predicate_id,r.unit)
    if signature(left) != signature(right):
        raise InvalidPlan('Compared results must use the same measurement, grain and unit')
    if operation.operation == 'ratio' and values[1] == 0:
        raise InvalidPlan('A ratio denominator cannot be zero')
    value = values[0]-values[1] if operation.operation == 'difference' else values[0]/values[1]
    observed = any(ref['population_basis'] == 'observed_subset' for ref in references)
    return EvidenceResult(output_id=operation.output_id,operation=operation,scope_fingerprint=scope.fingerprint(),
        label=f'{left.label} / {right.label}' if operation.operation == 'ratio' else f'{left.label} − {right.label}',
        unit=left.unit if operation.operation == 'difference' else 'ratio',rows=[{'value':str(value)}],
        status='partial' if observed else 'answered',coverage=Coverage(notes=['Computed from checked result cells.']),
        derivation={'claims':references,'basis':'observed_subset' if observed else 'exact_population'})


def verify_derived(result, results, scope):
    if result.derivation is None: raise ValueError('Derived result lacks operand provenance')
    rebuilt = derive(result.operation,results,scope)
    if (rebuilt.rows != result.rows or rebuilt.unit != result.unit or rebuilt.derivation != result.derivation
            or rebuilt.status != result.status):
        raise ValueError('Derived result differs from its checked operands')
    return rebuilt
