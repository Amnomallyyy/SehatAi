-- DietBot's own Supabase tables. These were never checked into the repo
-- (docs/migration.sql is empty) and don't exist yet in the shared
-- project -- recommender.py/session_manager.py will fail at runtime with
-- "relation does not exist" until this is run once, by hand, in the
-- Supabase SQL editor. patients/extracted_data/medicines/clinical_advice
-- already exist (shared with backend/ and datafetch/) and are untouched
-- here.

create extension if not exists pgcrypto;

create table if not exists diet_chat_sessions (
    id uuid primary key default gen_random_uuid(),
    patient_id uuid not null,
    title text not null,
    status text not null default 'active',
    created_at timestamptz not null default now()
);

create index if not exists diet_chat_sessions_patient_id_idx
    on diet_chat_sessions (patient_id);

create table if not exists diet_chat_messages (
    id uuid primary key default gen_random_uuid(),
    session_id uuid not null references diet_chat_sessions (id) on delete cascade,
    role text not null,
    content text not null,
    feedback_score integer not null default 0,
    created_at timestamptz not null default now()
);

create index if not exists diet_chat_messages_session_id_idx
    on diet_chat_messages (session_id);

create table if not exists diet_recommendations (
    id uuid primary key default gen_random_uuid(),
    patient_id uuid not null,
    session_id uuid references diet_chat_sessions (id) on delete set null,
    recommendation text,
    reasoning text,
    risk_score double precision,
    confidence_score double precision,
    safety_warnings jsonb,
    generated_at timestamptz not null default now()
);

create index if not exists diet_recommendations_patient_id_idx
    on diet_recommendations (patient_id);
