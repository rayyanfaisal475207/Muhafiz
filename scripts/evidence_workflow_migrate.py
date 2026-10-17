"""Apply additive evidence migrations; live upgrades require a fresh evidence backup."""
import argparse
import asyncio
from pathlib import Path
from urllib.parse import urlsplit

import asyncpg
from dotenv import dotenv_values
from datetime import datetime,timezone


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    target=parser.add_mutually_exclusive_group(required=True)
    target.add_argument('--test', action='store_true')
    target.add_argument('--live', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    cfg = dotenv_values(root/('.env.evidence-test' if args.test else '.env'))
    dsn = cfg.get('EVIDENCE_TEST_ADMIN_URL' if args.test else 'EVIDENCE_ADMIN_DATABASE_URL','')
    if not dsn:raise RuntimeError('Explicit administrative evidence credentials are required')
    dsn=dsn.replace('postgresql+asyncpg://','postgresql://')
    database = urlsplit(dsn).path
    if args.test and not database.startswith('/muhafiz_evidence_test_'):
        raise RuntimeError('Refusing a database outside the isolated test namespace')
    for key in ('EVIDENCE_QUERY_DATABASE_URL','EVIDENCE_INGEST_DATABASE_URL'):
        selected=urlsplit(cfg.get(key,''))
        administrative=urlsplit(dsn)
        if (selected.path,selected.hostname,selected.port or 5432)!=(administrative.path,administrative.hostname,administrative.port or 5432):
            raise RuntimeError('Administrative and runtime roles must name the same evidence database and server')
    conn = await asyncpg.connect(dsn,timeout=10)
    try:
        if not await conn.fetchval("SELECT to_regclass('evidence.documents')"):
            raise RuntimeError('Provision the original evidence migration before additive migrations')
        if args.live:
            from evidence_snapshot import snapshot
            destination=root/'data/qa/workflow_v2'/('pre_upgrade_'+datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S'))
            await snapshot(conn,destination)
            print('Fresh evidence backup verified before live additive upgrade.')
        if not await conn.fetchval("SELECT to_regclass('evidence.source_profiles')"):
            await conn.execute((root/'migrations/079_evidence_learned_catalog.sql').read_text(encoding='utf-8'))
        await conn.execute((root/'migrations/080_evidence_search_prerequisites.sql').read_text(encoding='utf-8'))
        if not await conn.fetchval("SELECT EXISTS(SELECT 1 FROM information_schema.columns WHERE table_schema='evidence' AND table_name='discovery_jobs' AND column_name='attempts')"):
            await conn.execute((root/'migrations/081_evidence_discovery_attempts.sql').read_text(encoding='utf-8'))
        if not await conn.fetchval("SELECT EXISTS(SELECT 1 FROM information_schema.columns WHERE table_schema='evidence' AND table_name='discovery_jobs' AND column_name='failed_attempts')"):
            await conn.execute((root/'migrations/082_evidence_discovery_failures.sql').read_text(encoding='utf-8'))
        await conn.execute((root/'migrations/083_evidence_attachment_revocation.sql').read_text(encoding='utf-8'))
        await conn.execute((root/'migrations/084_evidence_nested_bindings.sql').read_text(encoding='utf-8'))
        await conn.execute((root/'migrations/085_evidence_projections.sql').read_text(encoding='utf-8'))
        # 086 redefines roman_skeleton so one name typed in Urdu or in the
        # Arabic letter forms has one skeleton, and fixes a one-character
        # gap in 058's letter map. Listed here because this script is the
        # only thing that migrates the isolated test database: applying it
        # by hand to the live database alone is how the function silently
        # differed between what the tests check and what the system runs.
        # The file asserts its own letter map, so a misaligned edit fails
        # here rather than in a question.
        await conn.execute((root/'migrations/086_arabic_letter_forms_in_skeleton.sql').read_text(encoding='utf-8'))
        print('Evidence prerequisites ready; runtime selection and application processes unchanged.')
    finally:
        await conn.close()


if __name__ == '__main__': asyncio.run(main())
