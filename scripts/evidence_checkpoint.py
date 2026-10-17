"""Archive the current implementation without changing or discarding user work."""
import argparse
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import subprocess
from zipfile import ZipFile,ZIP_DEFLATED

ROOT=Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory',type=Path)
    args=parser.parse_args()
    args.directory.mkdir(parents=True,exist_ok=False)
    files=[]
    for folder in ['src','scripts','tests','migrations','docs','frontend/src','services']:
        files.extend(path for path in (ROOT/folder).rglob('*') if path.is_file()
                     and path.suffix in {'.py','.sql','.md','.json','.txt','.ts','.tsx','.css','.html'}
                     and '__pycache__' not in path.parts and path.name!='.env')
    files.extend(path for path in ROOT.glob('*') if path.is_file() and
                 (path.suffix in {'.md','.toml'} or path.name.startswith('requirements') and path.suffix=='.txt'))
    manifest={'created_at':datetime.now(timezone.utc).isoformat(),'files':{},
              'description':'Current checkpoint; preserves tracked and untracked implementation, excludes .env credentials',
              'git_status':subprocess.run(['git','status','--short'],cwd=ROOT,capture_output=True,text=True,check=True).stdout}
    with ZipFile(args.directory/'source.zip','x',compression=ZIP_DEFLATED) as archive:
        for path in sorted(set(files)):
            name=path.relative_to(ROOT).as_posix()
            content=path.read_bytes()
            manifest['files'][name]=hashlib.sha256(content).hexdigest()
            archive.writestr(name,content)
    manifest['archive_sha256']=hashlib.sha256((args.directory/'source.zip').read_bytes()).hexdigest()
    (args.directory/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'files':len(manifest['files']),'checkpoint':str(args.directory)}))


if __name__=='__main__':main()
