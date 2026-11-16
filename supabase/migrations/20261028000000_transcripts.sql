-- Run transcripts: one row per Inspect sample (trial), one row per message, and
-- the raw .eval log in a private bucket. Transcripts can contain the inbox used
-- for the run, so only the service role reads or writes them: row level
-- security is on with no policies, and the bucket is private.

create table public.runs (
  id uuid primary key default gen_random_uuid(),
  eval_id text not null,
  sample_id text not null,
  epoch integer not null,
  run_id text,
  eval_set_id text,
  task text not null,
  status text not null,
  error text,
  model text not null,
  model_roles jsonb,
  scenario text,
  task_args jsonb not null default '{}',
  eval_metadata jsonb not null default '{}',
  sample_metadata jsonb not null default '{}',
  git_commit text,
  started_at timestamptz,
  completed_at timestamptz,
  total_time double precision,
  stop_reason text,
  turns integer,
  sim_started_at timestamptz,
  sim_ended_at timestamptz,
  total_cost double precision,
  message_count integer not null,
  scores jsonb not null default '{}',
  model_usage jsonb not null default '{}',
  role_usage jsonb not null default '{}',
  world jsonb,
  counterparts jsonb not null default '{}',
  resume_state jsonb not null default '{}',
  long_horizon jsonb,
  eval_log_path text,
  eval_log_sha256 text,
  uploaded_at timestamptz not null default now(),
  unique (eval_id, sample_id, epoch)
);

create index runs_model_idx on public.runs (model);
create index runs_scenario_idx on public.runs (scenario);
create index runs_started_at_idx on public.runs (started_at desc);

-- message_index is the 0-based index used by grading and Scout citations ([M12]).
create table public.run_messages (
  run_id uuid not null references public.runs (id) on delete cascade,
  message_index integer not null,
  role text not null,
  text text not null,
  tool_calls jsonb,
  tool_call_id text,
  function text,
  message jsonb not null,
  primary key (run_id, message_index)
);

alter table public.runs enable row level security;
alter table public.run_messages enable row level security;

revoke all on public.runs, public.run_messages from anon, authenticated;
grant select, insert, update, delete on public.runs, public.run_messages to service_role;

insert into storage.buckets (id, name, public)
values ('eval-logs', 'eval-logs', false)
on conflict (id) do nothing;
