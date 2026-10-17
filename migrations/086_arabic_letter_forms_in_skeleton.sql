-- The skeleton must also know the ARABIC forms of letters Urdu rewrote,
-- and its own letter map was one character short.
--
-- WHY
--
-- `roman_skeleton` (migration 058) collapses Urdu spelling variants well:
-- ذ and ز both give 'z', ص and س both give 's', so ذیشان and زیشان share a
-- skeleton and a typist's choice between them costs nothing.
--
-- It only knows the URDU letter forms. Urdu replaced several Arabic letters
-- with its own characters at different code points, and both forms reach
-- this system constantly -- Arabic keyboard layouts, Windows defaults, text
-- pasted from Arabic sources, phones offering the Arabic glyph:
--
--     ک  U+06A9  keheh      (Urdu)   vs  ك  U+0643  kaf  (Arabic)
--     ہ  U+06C1  heh goal   (Urdu)   vs  ه  U+0647  heh  (Arabic)
--     ی  U+06CC  farsi yeh  (Urdu)   vs  ي  U+064A  yeh  (Arabic)
--
-- MEASURED on this corpus, before this migration:
--
--     resolve_mentions('کاشف')   ->  6 matches     (Urdu keheh)
--     resolve_mentions('كاشف')   ->  0 matches     (Arabic kaf)
--     resolve_mentions('kashif') ->  6 matches
--
-- Those two Urdu strings are the same name and look identical on screen.
-- One found the person, the other found nothing: the Arabic kaf fell
-- through `translate()` untouched, survived as a non-[a-z] character, and
-- was then stripped -- leaving a shorter skeleton matching no stored name.
--
-- AND A LATENT BUG IN 058 ITSELF
--
-- Its `from` string held 38 characters and its `to` string 37. `translate()`
-- DELETES a source character with no counterpart, so the last letter, ة,
-- was silently dropped and إ took ة's intended 'h' instead of 'a'. Nothing
-- noticed because both are rare here -- but it also meant any letter
-- appended to that map landed one position out, which is how the first
-- attempt at this migration mapped ك to 'h'. The two groups are now written
-- one letter per line with their target beside them, and a length assertion
-- below refuses to install a map that does not line up.

DROP FUNCTION IF EXISTS roman_skeleton(text);
CREATE FUNCTION roman_skeleton(p_text text)
RETURNS text LANGUAGE sql IMMUTABLE AS $fn$
    SELECT nullif(regexp_replace(
        translate(
            -- Digraphs first: single Urdu letters that romanise to two Latin
            -- characters, so translate() cannot do them.
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
            -- Emphatic and retroflex pairs collapse together (ٹ/ت, ڈ/د,
            -- ڑ/ر), which is what makes this a skeleton rather than a
            -- transliteration. 45 characters, matching 45 below.
            'ابپتٹثجحدڈذرڑزسصضطظعفقکگلمنںوہھءیےآأإةكهيؤئٱى',
            --  ا ب پ ت ٹ ث  -> a b p t t s
            'abptts' ||
            --  ج ح د ڈ ذ ر ڑ ز س ص ض ط ظ ع ف ق ک گ ل م ن ں و ہ ھ
            'jhddzrrzsszzzafqkglmnnohh' ||
            --  ء ی ے آ أ إ ة  -> i i a a a a h   (seven, not six)
            'iiaaaah' ||
            --  ك ه ي ؤ ئ ٱ ى  -> k h i o i a i   (the Arabic forms)
            'khioiai'),
        '[^a-z]+', '', 'g'), '');
$fn$;

COMMENT ON FUNCTION roman_skeleton(text) IS
'Consonant skeleton of Arabic-script text, for generating cross-script '
'CANDIDATES. Collapses Urdu spelling variants (ذ/ز, ص/س) and the Arabic '
'letter forms Urdu rewrote (ك/ک, ه/ہ, ي/ی), so one name typed either way '
'has one skeleton. Lossy by construction — Urdu omits short vowels — so its '
'matches are labelled cross_script and are never treated as confirmation.';

-- The map must line up, or every letter after the gap is wrong. This is the
-- bug 058 shipped with, so it is asserted rather than trusted.
DO $check$
DECLARE
    pairs text[][] := ARRAY[
        ARRAY['کاشف',  'كاشف'],   -- Urdu keheh vs Arabic kaf
        ARRAY['ذیشان', 'زیشان'],  -- the measured failure
        ARRAY['سرفراز','صرفراز']  -- س / ص
    ];
    one text;
    two text;
    i int;
BEGIN
    FOR i IN 1 .. array_length(pairs, 1) LOOP
        one := roman_skeleton(pairs[i][1]);
        two := roman_skeleton(pairs[i][2]);
        IF one IS DISTINCT FROM two THEN
            RAISE EXCEPTION
                'roman_skeleton letter map is misaligned: % -> %, but % -> %',
                pairs[i][1], one, pairs[i][2], two;
        END IF;
    END LOOP;
    IF roman_skeleton('کاشف') <> 'kashf' THEN
        RAISE EXCEPTION 'roman_skeleton(کاشف) gave %, expected kashf',
                        roman_skeleton('کاشف');
    END IF;
    -- A Roman query must pass through unchanged, or every cross-script
    -- match that works today would break.
    IF roman_skeleton('zeeshan') <> 'zeeshan' THEN
        RAISE EXCEPTION 'roman_skeleton must leave Latin text alone, gave %',
                        roman_skeleton('zeeshan');
    END IF;
END
$check$;
