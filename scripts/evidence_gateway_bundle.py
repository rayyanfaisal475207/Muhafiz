"""Build a reviewable GPU-host deployment bundle without credentials or case data."""
import argparse
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
from zipfile import ZipFile,ZIP_DEFLATED

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output',type=Path)
    args=parser.parse_args()
    files={name:(ROOT/name).read_bytes() for name in ['services/model_gateway/app.py',
        'services/model_gateway/README.md','tests/test_evidence_gateway.py']}
    packages=['fastapi','httpx','pydantic','uvicorn','pytest','pytest-asyncio']
    files['requirements-gateway.txt']=('\n'.join(name+'=='+version(name) for name in packages)+'\n').encode()
    manifest={'description':'Private GPU gateway; source code and synthetic tests only',
              'files':{name:hashlib.sha256(content).hexdigest() for name,content in files.items()},
              'entry_point':'python -m uvicorn services.model_gateway.app:app --host 127.0.0.1 --port 8081 --workers 1',
              'test_command':'python -m pytest tests/test_evidence_gateway.py -q'}
    files['bundle_manifest.json']=json.dumps(manifest,indent=2).encode()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with ZipFile(args.output,'x',compression=ZIP_DEFLATED) as archive:
        for name,content in files.items():archive.writestr(name,content)
    print(json.dumps({'bundle':str(args.output),'files':len(files),
                      'sha256':hashlib.sha256(args.output.read_bytes()).hexdigest()}))


if __name__=='__main__':main()
