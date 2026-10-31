-- One row, overwritten every five minutes. The phone reads it with the anon key.
create table if not exists public.estado (
  id         integer primary key,
  generated  timestamptz not null,
  levels     jsonb not null,
  incidents  jsonb not null,
  sources    jsonb not null,
  updated_at timestamptz not null default now()
);

alter table public.estado enable row level security;

create policy "anyone can read estado"
  on public.estado for select
  using (true);

-- Writes come from publish.py with the service key, which bypasses RLS.

-- Optional: keep history for the "line health" ideas later.
create table if not exists public.estado_history (
  generated  timestamptz primary key,
  levels     jsonb not null,
  incidents  jsonb not null
);

create or replace function public.estado_archive() returns trigger as $$
begin
  insert into public.estado_history (generated, levels, incidents)
  values (new.generated, new.levels, new.incidents)
  on conflict (generated) do nothing;
  return new;
end;
$$ language plpgsql;

drop trigger if exists estado_archive on public.estado;
create trigger estado_archive after insert or update on public.estado
  for each row execute function public.estado_archive();
