-- Apply once in Supabase SQL Editor. Private data; no public read/write policies.
-- This does NOT enable client-side access. Store the service-role key only in Streamlit Secrets.
create table if not exists public.student_problems (
  problem_id text primary key,
  created_at timestamptz not null default now(),
  initial_question text not null,
  file_path text,
  file_sha256 text
);
create table if not exists public.student_turns (
  turn_id text primary key,
  problem_id text not null references public.student_problems(problem_id) on delete restrict,
  created_at timestamptz not null default now(),
  role text not null check (role in ('학생', 'AI', '시스템')),
  content text not null,
  status text not null default 'ok'
);
create index if not exists student_turns_problem_created_idx
  on public.student_turns(problem_id, created_at);
alter table public.student_problems enable row level security;
alter table public.student_turns enable row level security;
-- No anon/authenticated policies: both tables inaccessible to public API keys.
revoke all on public.student_problems, public.student_turns from anon, authenticated;
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('student-questions', 'student-questions', false, 10485760,
        array['image/png','image/jpeg','application/pdf'])
on conflict (id) do update set public = false;
-- Do not create any public storage.objects policies.
