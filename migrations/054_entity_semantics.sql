-- ============================================================
-- 054 — Say what each table and column MEANS
--
-- WHY
--
-- The extraction contract is generated from the schema, so the model sees
-- exactly what the database can tell it. Today that is column names and
-- types, and it is not enough. Measured on one case:
--
--   event_type_raw = 'Crime scene'    -- a place, filed as an event
--   event_type_raw = NULL             -- three rows, description only
--
-- A column called event_type_raw does not say that an event is something
-- that HAPPENED at a moment, or that an address is not one. The model
-- guessed, reasonably, and was wrong.
--
-- IS THIS HARDCODING?
--
-- No, and the distinction is the same one 051 drew for the catalog. A
-- comment states what a table IS FOR. It does not enumerate what may go
-- inside it. 'case_events holds things that happened' constrains nothing
-- about which events exist — arrest, bail, recovery, or a kind of event
-- first seen next year all land in event_type_raw exactly as written.
--
-- This is the metadata layer: structure the model can read, with no
-- vocabulary attached.
--
-- The comments also serve the planner, since describe_table() surfaces
-- them, and they serve the next person to read the schema.
-- ============================================================

BEGIN;

-- ── Entities ────────────────────────────────────────────────────────
COMMENT ON TABLE persons IS
'A human being mentioned in a case document. One row per MENTION, not per '
'human: two rows may turn out to be the same person, and merging them is '
'identity resolution, which requires evidence and lives in '
'person_identity_links. Never merged on a shared name.';

COMMENT ON COLUMN persons.canonical_name_ur IS
'The name exactly as the document writes it, in its original script. Not '
'transliterated and not normalised.';
COMMENT ON COLUMN persons.father_name_ur IS
'Father name, the standard Pakistani disambiguator, written as "ولد X" or '
'"بنت X". Recorded only when the document states it.';
COMMENT ON COLUMN persons.cnic IS
'National identity number, 13 digits usually punctuated 00000-0000000-0. '
'The strongest identity signal available; never invent or complete one.';

COMMENT ON TABLE case_parties IS
'The link between a person and a case, carrying the ROLE that person plays '
'in it: accused, complainant, witness, victim — in whatever word the '
'document uses. The role belongs here and not on the person, because the '
'same person is an accused in one case and a witness in another, and may '
'change role within one case as the file develops.';

COMMENT ON COLUMN case_parties.role_raw IS
'Role exactly as written: ملزم, مدعی, گواہ, accused, complainant. Never '
'translated or mapped to a category.';
COMMENT ON COLUMN case_parties.age_at_incident IS
'Age at the time of the incident, not current age — which is why it sits '
'on the party rather than on the person.';

-- ── Events: the table that was being filled with non-events ─────────
COMMENT ON TABLE case_events IS
'Something that HAPPENED in the life of a case, at a moment in time: an '
'arrest, a bail order, a recovery, a transfer, a hearing, a challan being '
'filed. A row here must name an occurrence. A place is not an event; a '
'description of injuries is not an event; the current position of a file '
'is not an event unless something changed. If the text does not describe '
'something happening, it does not belong in this table.';

COMMENT ON COLUMN case_events.event_type_raw IS
'What happened, in the document''s own words: گرفتاری, ضمانت, برآمدگی, '
'arrest, bail, recovery. REQUIRED — an event with no type cannot answer '
'any question about a sequence, which is the entire purpose of this table.';
COMMENT ON COLUMN case_events.occurred_at IS
'When it happened in the world. Frequently absent in this corpus; absent '
'is correct and is never to be guessed from the document date.';
COMMENT ON COLUMN case_events.stage_order IS
'Explicit position in a person''s journey when the text states an order '
'but no dates, which is common: "گرفتار، بعد ازاں ضمانت" is a sequence '
'without timestamps.';

-- ── Physical evidence ───────────────────────────────────────────────
COMMENT ON TABLE weapons IS
'A weapon named in a case: firearm, blade, blunt instrument. One row per '
'weapon, not per mention of it.';
COMMENT ON COLUMN weapons.weapon_type_raw IS
'The weapon as written: پستول, 30 بور پستول, رائفل, چھری. REQUIRED — the '
'kind of weapon is the fact; quantity and calibre only describe it.';
COMMENT ON COLUMN weapons.caliber_or_bore IS
'Calibre or bore when stated, e.g. "30 بور", ".30", "9mm". Recorded '
'separately from the weapon type so it can be counted.';
COMMENT ON COLUMN weapons.license_state_raw IS
'What the document says about licensing, e.g. بغیر لائسنس (unlicensed). '
'Stored in the document''s own words, because filtering an English guess '
'against an Urdu value is the failure that started this rebuild.';

COMMENT ON TABLE seized_items IS
'Property taken into custody that is not a weapon or a vehicle: cash, '
'phones, documents, stolen goods. What was seized must be described; a '
'quantity alone is not a seizure.';

COMMENT ON TABLE vehicles IS
'A vehicle involved in a case, whether used, stolen, or recovered.';

COMMENT ON TABLE phone_numbers IS
'A phone number appearing in a case, optionally tied to a person. Stored '
'as written, since formatting varies and the digits are what matter.';

-- ── Statutes and officers ───────────────────────────────────────────
COMMENT ON TABLE acts IS
'A piece of legislation, discovered from the documents rather than listed: '
'PPC, PECA 2016, Arms Ordinance 1965, Illegal Dispossession Act 2005.';
COMMENT ON TABLE statute_sections IS
'One section within an act, e.g. 328A of PPC. The unit that is actually '
'applied to a case.';
COMMENT ON TABLE case_statutes IS
'Which sections were applied to which case, and at what stage. Populated '
'deterministically from the document header, so it carries no extraction '
'risk.';

COMMENT ON TABLE officers IS
'A police officer named in a case: investigating officer, arresting '
'officer, station house officer.';
COMMENT ON COLUMN officers.rank_raw IS
'Rank as written: ASI, SI, Inspector, محرر. Never mapped to a scale.';

COMMENT ON TABLE case_documents IS
'The stream of documents making up a case file, in order. An FIR opens it; '
'zimni (investigation reports), chalaan, and court documents continue it. '
'A case is this stream, not a single record.';

COMMENT ON TABLE places IS
'Any geographic or organisational unit at any level: country, province, '
'city, district, police station. The LEVEL is a value in kind_raw, not a '
'table, so depth is unlimited and a new level is an INSERT.';

COMMENT ON TABLE rebuild_cases IS
'One criminal case, opened by an FIR. Holds only what the opening document '
'established; everything that happens afterwards lives in case_events and '
'case_documents, because a case is a process and not a fixed record.';

COMMIT;
