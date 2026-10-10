-- =====================================================================
-- SehatAI -- shared Supabase schema (Postgres + pgvector + Storage)
--
-- Run once in a NEW Supabase project: Dashboard -> SQL Editor -> paste ->
-- Run. Idempotent: safe to re-run (CREATE ... IF NOT EXISTS / OR REPLACE).
--
-- Why this file exists: none of the tables below were ever checked into
-- the repo (docs/migration.sql was empty) -- they were created by hand in
-- one Supabase project, so losing that project meant losing the schema.
-- This is reconstructed from every read/write in the code that uses them:
--   sehatai/*.js        patients, medicines, extracted_data, intake form,
--                       chat_sessions/state/pointers, patient_api_tokens,
--                       summaries_vectors + match_patient_history()
--   datafetch/*.py      documents, extracted_data, medicines,
--                       clinical_advice, atomic_upsert_document(),
--                       get_patient_timeline(), Storage bucket
--   dietbot/*.py        diet_* tables (dietbot/schema.sql, merged here)
--   backend/app/models  the same bridge tables, read via SQLAlchemy
--
-- CareLink's OWN tables (users, connections, conversations, messages,
-- reports, ...) are NOT here: the core API creates them itself on startup
-- (Base.metadata.create_all in backend/app/main.py).
--
-- Security: row-level security is ENABLED on every table with no
-- policies. Every service talks to Supabase with the service-role key or
-- the postgres connection string, which bypass RLS -- so the apps work
-- unchanged, while the public anon key (shipped to browsers by Supabase
-- tooling) can read nothing.
-- =====================================================================

create extension if not exists pgcrypto;
create extension if not exists vector;

-- ------------------------------------------------------------ patients
create table if not exists patients (
    id              uuid primary key default gen_random_uuid(),
    name            text,
    date_of_birth   date,
    age             integer,
    sex             text,
    -- DataFetch's consent gate: no document is processed while this is null.
    consented_at    timestamptz,
    -- bcrypt hash checked by datafetch/auth.py (see backend lab_reports.py
    -- for why CareLink rotates a throwaway one per upload).
    password_hash   text,
    created_at      timestamptz not null default now()
);

create table if not exists patient_api_tokens (
    token_hash  text primary key,               -- sha256(raw token), never the raw token
    patient_id  uuid not null references patients (id) on delete cascade,
    created_at  timestamptz not null default now(),
    revoked     boolean not null default false
);
create index if not exists patient_api_tokens_patient_idx on patient_api_tokens (patient_id);

create table if not exists patient_intake_form (
    patient_id          uuid primary key references patients (id) on delete cascade,
    existing_conditions text[] not null default '{}',
    allergies           text[] not null default '{}',
    current_medications text[] not null default '{}',
    family_history      text[] not null default '{}',
    updated_at          timestamptz not null default now()
);

-- ----------------------------------------------- documents + extraction
create table if not exists documents (
    id                uuid primary key default gen_random_uuid(),
    patient_id        uuid not null references patients (id) on delete cascade,
    category          text,
    uploaded_at       timestamptz not null default now(),
    doctor_reviewed   boolean not null default false,
    file_path         text,          -- object path inside the medical-documents bucket
    file_url          text,
    file_size_bytes   bigint,
    mime_type         text,
    original_filename text,
    file_hash         text,
    document_date     date,
    status            text,
    raw_ocr           text,
    ocr_engine        text,
    ocr_confidence    numeric,
    ai_summary        text,
    doctor_notes      text,
    superseded_by     uuid references documents (id) on delete set null
);
create index if not exists documents_patient_idx on documents (patient_id, document_date desc);
-- DataFetch's duplicate check is (patient_id, file_hash).
create unique index if not exists documents_patient_hash_uq on documents (patient_id, file_hash) where file_hash is not null;

create table if not exists extracted_data (
    id              uuid primary key default gen_random_uuid(),
    document_id     uuid not null references documents (id) on delete cascade,
    patient_id      uuid references patients (id) on delete cascade,
    test_name       text not null,
    value           text not null,
    value_numeric   numeric,
    unit            text,
    normal_range    text,
    flag            text,
    operator        text,
    recorded_at     timestamptz not null default now(),
    document_date   date,
    supersedes_id   uuid,
    ocr_engine      text,
    ocr_confidence  numeric,
    created_at      timestamptz not null default now()
);
create index if not exists extracted_data_patient_idx on extracted_data (patient_id, recorded_at desc);
create index if not exists extracted_data_document_idx on extracted_data (document_id);

create table if not exists medicines (
    id           uuid primary key default gen_random_uuid(),
    patient_id   uuid not null references patients (id) on delete cascade,
    document_id  uuid references documents (id) on delete set null,
    name         text not null,
    dosage       text,
    start_date   date,
    end_date     date,
    active       boolean not null default true,
    recorded_at  timestamptz not null default now()
);
create index if not exists medicines_patient_idx on medicines (patient_id, active);

-- Portal-owned (also auto-created by the backend's create_all): which
-- CareLink doctor prescribed a medicines row, plus an optional file.
-- Guarded because `users` is created by the backend, not by this file.
do $$
begin
    if to_regclass('public.users') is not null then
        create table if not exists medicine_prescriptions (
            id               serial primary key,
            medicine_id      uuid not null unique references medicines (id) on delete cascade,
            doctor_id        integer not null references users (id),
            notes            text,
            attachment_path  varchar(500),
            attachment_name  varchar(255),
            attachment_mime  varchar(100),
            created_at       timestamp not null default now(),
            updated_at       timestamp not null default now()
        );
    end if;
end $$;

create table if not exists clinical_advice (
    id            uuid primary key default gen_random_uuid(),
    patient_id    uuid not null references patients (id) on delete cascade,
    document_id   uuid references documents (id) on delete set null,
    content       text not null,
    origin        text,              -- 'entered' | 'extracted' | ...
    document_date date,
    recorded_at   timestamptz not null default now()
);
create index if not exists clinical_advice_patient_idx on clinical_advice (patient_id, recorded_at desc);

-- RAG memory for the symptom assistant (sehatai/getPatientdata.js).
-- jina-embeddings-v3 at 1024 dimensions (sehatai/embeddingProvider.js).
create table if not exists summaries_vectors (
    id           uuid primary key default gen_random_uuid(),
    patient_id   uuid not null references patients (id) on delete cascade,
    source_id    uuid,
    source_type  text not null,      -- 'clinical_advice' | 'ai_summary'
    content      text not null,
    embedding    vector(1024),
    created_at   timestamptz not null default now()
);
create index if not exists summaries_vectors_patient_idx on summaries_vectors (patient_id);
create index if not exists summaries_vectors_embedding_idx on summaries_vectors using hnsw (embedding vector_cosine_ops);

-- Vocabulary the grounding verifier allows in assistant replies.
create table if not exists specialists (
    id    bigint generated always as identity primary key,
    name  text not null unique
);
create table if not exists symptom_related_tests (
    id         bigint generated always as identity primary key,
    test_name  text not null unique
);

-- ------------------------------------------- symptom assistant sessions
create table if not exists chat_sessions (
    id          uuid primary key default gen_random_uuid(),
    patient_id  uuid not null references patients (id) on delete cascade,
    started_at  timestamptz not null default now()
);
create index if not exists chat_sessions_patient_idx on chat_sessions (patient_id);

create table if not exists chat_session_state (
    session_id  uuid primary key references chat_sessions (id) on delete cascade,
    state       jsonb not null,
    updated_at  timestamptz not null default now()
);

create table if not exists active_session_pointers (
    patient_id  text not null,
    mode        text not null,
    session_id  uuid not null references chat_sessions (id) on delete cascade,
    primary key (patient_id, mode)
);

-- -------------------------------------------------------------- DietBot
create table if not exists diet_chat_sessions (
    id          uuid primary key default gen_random_uuid(),
    patient_id  uuid not null,
    title       text not null,
    status      text not null default 'active',
    created_at  timestamptz not null default now()
);
create index if not exists diet_chat_sessions_patient_id_idx on diet_chat_sessions (patient_id);

create table if not exists diet_chat_messages (
    id              uuid primary key default gen_random_uuid(),
    session_id      uuid not null references diet_chat_sessions (id) on delete cascade,
    role            text not null,
    content         text not null,
    feedback_score  integer not null default 0,
    created_at      timestamptz not null default now()
);
create index if not exists diet_chat_messages_session_id_idx on diet_chat_messages (session_id);

create table if not exists diet_recommendations (
    id                uuid primary key default gen_random_uuid(),
    patient_id        uuid not null,
    session_id        uuid references diet_chat_sessions (id) on delete set null,
    recommendation    text,
    reasoning         text,
    risk_score        double precision,
    confidence_score  double precision,
    safety_warnings   jsonb,
    generated_at      timestamptz not null default now()
);
create index if not exists diet_recommendations_patient_id_idx on diet_recommendations (patient_id);

create table if not exists diet_patient_preferences (
    id                    uuid primary key default gen_random_uuid(),
    patient_id            uuid not null unique,
    dietary_restrictions  text[] not null default '{}',
    food_allergies        text[] not null default '{}',
    disliked_foods        text[] not null default '{}',
    favorite_foods        text[] not null default '{}',
    updated_at            timestamptz not null default now()
);

-- Links a patient's current symptom-assistant session to their DietBot
-- session (sehatai/chatLog.js).
create table if not exists diet_session_pointers (
    patient_id          text primary key,
    diet_session_id     uuid,
    sehatai_session_id  uuid references chat_sessions (id) on delete set null,
    updated_at          timestamptz not null default now()
);

-- ============================================== upgrade pre-existing tables
-- If the core API started before this script ran, its SQLAlchemy
-- create_all() already made bare versions of these four bridge tables
-- (client-side uuid defaults, fewer columns) and the CREATE TABLE IF NOT
-- EXISTS above skipped them. Bring any such table up to the full shape.
alter table patients alter column id set default gen_random_uuid();
alter table patients add column if not exists consented_at timestamptz;
alter table patients add column if not exists password_hash text;
alter table patients add column if not exists created_at timestamptz not null default now();

alter table patient_api_tokens alter column created_at set default now();

alter table documents alter column id set default gen_random_uuid();
alter table documents alter column uploaded_at set default now();
alter table documents add column if not exists ocr_confidence numeric;
alter table documents add column if not exists ai_summary text;
alter table documents add column if not exists doctor_notes text;

alter table extracted_data alter column id set default gen_random_uuid();
alter table extracted_data alter column recorded_at set default now();
alter table extracted_data add column if not exists created_at timestamptz not null default now();

-- ============================================================ functions

-- DataFetch's single write (datafetch/pipeline.py step 9): the document
-- row, every extracted value and medicine, and the RAG embedding --
-- all-or-nothing in one transaction. Returns the document id; a re-run
-- for the same (patient_id, file_hash) returns the existing id.
create or replace function atomic_upsert_document(payload jsonb)
returns uuid
language plpgsql
security definer
set search_path = public
as $$
declare
    v_patient_id  uuid := (payload ->> 'patient_id')::uuid;
    v_file_url    text := payload ->> 'file_url';
    v_file_hash   text := payload ->> 'file_hash';
    v_doc_date    date := nullif(payload ->> 'document_date', '')::date;
    v_engine      text := payload ->> 'ocr_engine';
    v_confidence  numeric := nullif(payload ->> 'ocr_confidence', '')::numeric;
    v_file_path   text;
    v_doc_id      uuid;
    v_item        jsonb;
begin
    if v_patient_id is null then
        raise exception 'payload.patient_id is required';
    end if;

    select id into v_doc_id from documents
     where patient_id = v_patient_id and file_hash = v_file_hash
     limit 1;
    if v_doc_id is not null then
        return v_doc_id;
    end if;

    -- ".../object/public/medical-documents/<path>" -> "<path>"
    v_file_path := nullif(split_part(coalesce(v_file_url, ''), '/medical-documents/', 2), '');

    insert into documents (
        patient_id, category, status, file_url, file_path, file_hash, mime_type,
        document_date, raw_ocr, ocr_engine, ocr_confidence, ai_summary, doctor_notes
    ) values (
        v_patient_id,
        payload ->> 'category',
        'structured',
        v_file_url,
        split_part(v_file_path, '?', 1),
        v_file_hash,
        case lower(substring(v_file_path from '\.([A-Za-z0-9]+)(\?|$)'))
            when 'pdf'  then 'application/pdf'
            when 'png'  then 'image/png'
            when 'jpg'  then 'image/jpeg'
            when 'jpeg' then 'image/jpeg'
            when 'gif'  then 'image/gif'
            when 'tif'  then 'image/tiff'
            when 'tiff' then 'image/tiff'
            when 'bmp'  then 'image/bmp'
            else null
        end,
        v_doc_date,
        payload ->> 'raw_ocr',
        v_engine,
        v_confidence,
        payload ->> 'ai_summary',
        payload ->> 'doctor_notes'
    )
    returning id into v_doc_id;

    for v_item in select * from jsonb_array_elements(coalesce(payload -> 'extracted_values', '[]'::jsonb)) loop
        insert into extracted_data (
            document_id, patient_id, test_name, value, value_numeric, unit,
            normal_range, flag, operator, document_date, ocr_engine, ocr_confidence
        ) values (
            v_doc_id, v_patient_id,
            v_item ->> 'test_name',
            coalesce(v_item ->> 'value', ''),
            nullif(v_item ->> 'value_numeric', '')::numeric,
            v_item ->> 'unit',
            v_item ->> 'normal_range',
            v_item ->> 'flag',
            v_item ->> 'operator',
            v_doc_date, v_engine, v_confidence
        );
    end loop;

    for v_item in select * from jsonb_array_elements(coalesce(payload -> 'medicines', '[]'::jsonb)) loop
        insert into medicines (patient_id, document_id, name, dosage, start_date, end_date, active)
        values (
            v_patient_id, v_doc_id,
            v_item ->> 'name',
            v_item ->> 'dosage',
            nullif(v_item ->> 'start_date', '')::date,
            nullif(v_item ->> 'end_date', '')::date,
            coalesce((v_item ->> 'active')::boolean, true)
        );
    end loop;

    if nullif(payload ->> 'doctor_notes', '') is not null then
        insert into clinical_advice (patient_id, document_id, content, origin, document_date)
        values (v_patient_id, v_doc_id, payload ->> 'doctor_notes', 'extracted', v_doc_date);
    end if;

    if jsonb_typeof(payload -> 'embedding') = 'array' then
        insert into summaries_vectors (patient_id, source_id, source_type, content, embedding)
        values (
            v_patient_id, v_doc_id, 'ai_summary',
            coalesce(payload ->> 'embedding_content', payload ->> 'ai_summary', ''),
            (payload ->> 'embedding')::vector
        );
    end if;

    return v_doc_id;
end;
$$;

-- datafetch/history.py: one row per document with per-document counts.
create or replace function get_patient_timeline(p_patient_id uuid)
returns table (
    id               uuid,
    document_date    date,
    uploaded_at      timestamptz,
    category         text,
    extracted_count  bigint,
    medicines_count  bigint,
    advice_count     bigint
)
language sql
stable
set search_path = public
as $$
    select d.id, d.document_date, d.uploaded_at, d.category,
           (select count(*) from extracted_data e where e.document_id = d.id),
           (select count(*) from medicines m where m.document_id = d.id),
           (select count(*) from clinical_advice c where c.document_id = d.id)
      from documents d
     where d.patient_id = p_patient_id
     order by d.document_date desc nulls last, d.uploaded_at desc;
$$;

-- sehatai/getPatientdata.js: nearest patient-history snippets.
create or replace function match_patient_history(
    query_embedding  vector(1024),
    match_patient_id uuid,
    match_count      integer default 8
)
returns table (
    id           uuid,
    source_type  text,
    content      text,
    similarity   double precision,
    created_at   timestamptz
)
language sql
stable
set search_path = public
as $$
    select s.id, s.source_type, s.content,
           1 - (s.embedding <=> query_embedding) as similarity,
           s.created_at
      from summaries_vectors s
     where s.patient_id = match_patient_id
       and s.embedding is not null
     order by s.embedding <=> query_embedding
     limit match_count;
$$;

-- datafetch/setup_checks.py connectivity probe (supabase.rpc('version')).
create or replace function public.version()
returns text
language sql
stable
as $$ select pg_catalog.version(); $$;

-- ============================================================== storage
-- Private bucket: files are only ever served through the core API's
-- authenticated /structured/documents/{id}/file route.
insert into storage.buckets (id, name, public)
values ('medical-documents', 'medical-documents', false)
on conflict (id) do nothing;

-- ================================================================== RLS
do $$
declare t text;
begin
    foreach t in array array[
        'patients', 'patient_api_tokens', 'patient_intake_form', 'documents',
        'extracted_data', 'medicines', 'clinical_advice', 'summaries_vectors',
        'specialists', 'symptom_related_tests', 'chat_sessions',
        'chat_session_state', 'active_session_pointers', 'diet_chat_sessions',
        'diet_chat_messages', 'diet_recommendations', 'diet_patient_preferences',
        'diet_session_pointers'
    ] loop
        execute format('alter table %I enable row level security', t);
    end loop;
end;
$$;

-- RPCs are server-only too.
revoke execute on function atomic_upsert_document(jsonb) from anon, authenticated;
revoke execute on function get_patient_timeline(uuid) from anon, authenticated;
revoke execute on function match_patient_history(vector, uuid, integer) from anon, authenticated;

-- ====================================== portal accounts / scheduling / safety
-- CareLink-owned tables. The backend's create_all creates these on startup
-- (and enables RLS), so running this file is OPTIONAL -- it documents them and
-- lets you create them from the Supabase SQL editor instead. Nothing here
-- alters an existing table. Guarded because `users` / `appointments` are
-- created by the backend, not by this file.
do $$
begin
    if to_regclass('public.users') is not null and to_regclass('public.appointments') is not null then
        -- city / country collected at sign-up (patients and doctors)
        create table if not exists user_locations (
            user_id     integer primary key references users (id) on delete cascade,
            city        varchar(100) not null,
            country     varchar(100) not null,
            updated_at  timestamp not null default now()
        );

        -- a row = this account's email is not confirmed yet. Accounts that
        -- predate email confirmation have no row, so they count as confirmed.
        create table if not exists unconfirmed_users (
            user_id     integer primary key references users (id) on delete cascade,
            created_at  timestamp not null default now()
        );

        -- one-time emailed tokens; only the SHA-256 hash is stored
        create table if not exists auth_tokens (
            id          serial primary key,
            user_id     integer not null references users (id) on delete cascade,
            purpose     varchar(30) not null,           -- confirm_email | reset_password
            token_hash  varchar(64) not null unique,
            expires_at  timestamp not null,
            used_at     timestamp,
            created_at  timestamp not null default now()
        );
        create index if not exists auth_tokens_user_idx on auth_tokens (user_id);

        create table if not exists emergency_contacts (
            user_id       integer primary key references users (id) on delete cascade,
            name          varchar(200) not null,
            relationship  varchar(100) not null,
            email         varchar(255) not null,
            phone         varchar(50),
            updated_at    timestamp not null default now()
        );

        -- audit + rate limiting; never stores chat text
        create table if not exists emergency_alerts (
            id             serial primary key,
            patient_id     integer not null references users (id) on delete cascade,
            contact_email  varchar(255) not null,
            trigger        varchar(20) not null,        -- button | triage
            category       varchar(100),
            status         varchar(20) not null,        -- sent | dry_run | failed
            error          text,
            created_at     timestamp not null default now()
        );
        create index if not exists emergency_alerts_patient_idx on emergency_alerts (patient_id, created_at);

        -- doctor availability; booking creates a normal appointments row
        create table if not exists appointment_slots (
            id              serial primary key,
            doctor_id       integer not null references users (id) on delete cascade,
            starts_at       timestamp not null,
            ends_at         timestamp not null,
            status          varchar(10) not null default 'open',   -- open | booked
            appointment_id  integer unique references appointments (id) on delete set null,
            created_at      timestamp not null default now(),
            constraint uq_slot_doctor_start unique (doctor_id, starts_at)
        );
        create index if not exists appointment_slots_open_idx on appointment_slots (doctor_id, status, starts_at);

        -- one row per line of the intake form's tables (allergies, existing
        -- conditions, family history). detail1..3 meaning depends on `section`:
        --   allergy   name=allergen,  detail1=reaction, detail2=severity
        --   condition name=condition, detail1=since,    detail2=status
        --   family    name=condition, detail1=relative
        -- The bots keep reading patient_intake_form; saving the form refreshes
        -- those lists from the names here.
        create table if not exists intake_entries (
            id          serial primary key,
            user_id     integer not null references users (id) on delete cascade,
            section     varchar(20) not null,           -- allergy | condition | family
            position    integer not null default 0,
            name        varchar(200) not null,
            detail1     varchar(100),
            detail2     varchar(100),
            detail3     varchar(100),
            created_at  timestamp not null default now()
        );
        create index if not exists intake_entries_user_idx on intake_entries (user_id, section, position);

        -- top health concerns (ranked, up to 5) + when the main problem began
        create table if not exists intake_profile (
            user_id        integer primary key references users (id) on delete cascade,
            concerns       json not null default '[]',
            concern_began  varchar(100),
            updated_at     timestamp not null default now()
        );
    end if;
end $$;
