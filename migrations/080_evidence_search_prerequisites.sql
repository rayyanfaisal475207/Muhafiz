-- Evidence search prerequisites independent of legacy orchestration.
BEGIN;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE OR REPLACE FUNCTION public.roman_skeleton(p_text text)
RETURNS text LANGUAGE sql IMMUTABLE AS $fn$
    SELECT nullif(regexp_replace(
        translate(
            -- Digraphs first: these are single Urdu letters that romanise
            -- to two Latin characters, so translate() cannot do them.
            regexp_replace(
                regexp_replace(
                    regexp_replace(
                        regexp_replace(
                            regexp_replace(lower(coalesce(p_text, '')),
                                           'چ', 'ch', 'g'),
                            'خ', 'kh', 'g'),
                        'ش', 'sh', 'g'),
                    'غ', 'gh', 'g'),
                'ژ', 'zh', 'g'),
            -- Remaining letters, one Latin character each. Emphatic and
            -- retroflex pairs collapse together (ٹ/ت, ڈ/د, ڑ/ر), which is
            -- what makes this a skeleton rather than a transliteration.
            'ابپتٹثجحدڈذرڑزسصضطظعفقکگلمنںوہھءیےآأإة',
            'abptts'  ||
            'jhddzrrzsszzzafqkglmnnohh' ||
            'iiaaah'),
        '[^a-z]+', '', 'g'), '');
$fn$;
GRANT EXECUTE ON FUNCTION public.roman_skeleton(text) TO muhafiz_evidence_reader,muhafiz_evidence_ingestor;
COMMIT;
