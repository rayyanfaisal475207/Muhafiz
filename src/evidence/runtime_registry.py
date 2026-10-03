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
            raise RuntimeError('The frozen evidence kernel does not match its reviewed manifest')
        from .runtime_v2_0 import Runtime
    else:raise ValueError('Unsupported EVIDENCE_WORKFLOW_VERSION; no legacy fallback is allowed')
    instance=Runtime(store,model)
    instance.workflow_version=version
    return instance
