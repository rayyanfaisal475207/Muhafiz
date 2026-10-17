"""Export diagnostic answers for independent review; never invent correctness.

Input source inventory is {document_uuid: original_text}. Mapping JSON contains
{question_id: {scenario_id, language, attempt}}. Existing probes without a saved
scope cannot be certified and are refused. Output reviews deliberately remain
unset so the acceptance scorer fails until independent review is supplied.
"""
import argparse
import hashlib
import json
from pathlib import Path


def export(directory,mapping,sources):
    manifest=json.loads((directory/'manifest.json').read_text(encoding='utf-8'))
    if not manifest.get('scope'):raise ValueError('Diagnostic manifest did not persist its scope; rerun rather than inventing authorization context')
    rows=[]
    for result in json.loads((directory/'results.json').read_text(encoding='utf-8')):
        decision=mapping.get(str(result['question_id']))
        if decision is None:continue
        if 'answer' not in result:raise ValueError('A failed transport has no full answer to export')
        rows.append({**decision,'question':result['question'],'scope':manifest['scope'],
                     'answer':result['answer'],'sources':sources,'review':{},
                     'mode':manifest['mode'],'original_session_context':manifest.get('original_session_context',False),
                     'source_hashes':manifest['source_hashes'],
                     'source_inventory_sha256':hashlib.sha256(json.dumps(sources,sort_keys=True,ensure_ascii=False).encode()).hexdigest()})
    return rows


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory',type=Path)
    parser.add_argument('--mapping',type=Path,required=True)
    parser.add_argument('--sources',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    rows=export(args.directory,json.loads(args.mapping.read_text(encoding='utf-8')),
                json.loads(args.sources.read_text(encoding='utf-8')))
    with args.output.open('x',encoding='utf-8') as output:
        for row in rows:output.write(json.dumps(row,ensure_ascii=False)+'\n')
    print(json.dumps({'records':len(rows),'review_required':True,'release_ready':False}))


if __name__=='__main__':main()
