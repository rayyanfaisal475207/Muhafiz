"""Server-selected compatible evidence kernels; never a fallback to legacy SQL."""
import hashlib
import json
import os
from pathlib import Path


def build_runtime(store,model=None):
    version=os.getenv('EVIDENCE_WORKFLOW_VERSION','2.1')
    if version in {'2','2.1'}:
        from .runtime import Runtime
        version='2.1'
    elif version=='2.0':
        root=Path(__file__).resolve().parent
        manifest=json.loads((root/'runtime_versions.json').read_text(encoding='utf-8'))
        expected=manifest['2.0']['sha256']
        if hashlib.sha256((root/'runtime_v2_0.py').read_bytes()).hexdigest()!=expected:
            # MEASURED, 16 October 2026: the sealed hash names bytes that
            # exist nowhere. Only one commit ever touched runtime_v2_0.py,
            # and neither its committed form nor any line-ending variant of
            # it hashes to the manifest value -- the seal was taken from a
            # working tree that was never committed. So the reviewed copy is
            # unrecoverable, and 2.0 cannot be verified by anyone.
            #
            # It stays REFUSED rather than re-sealed. Re-sealing would make
            # the manifest attest to a review that never happened, which is
            # worse than losing the version: the whole point of the seal is
            # that this kernel is the one that was read and approved. 2.1 is
            # what every request uses, so nothing is lost by refusing.
            raise RuntimeError(
                'The frozen evidence kernel does not match its reviewed '
                'manifest, and no commit in this repository contains the '
                'sealed bytes, so it cannot be verified. Run 2.1, or re-seal '
                'runtime_versions.json deliberately after reviewing '
                'runtime_v2_0.py as it now stands.')
        from .runtime_v2_0 import Runtime
    else:raise ValueError('Unsupported EVIDENCE_WORKFLOW_VERSION; no legacy fallback is allowed')
    instance=Runtime(store,model)
    instance.workflow_version=version
    return instance
