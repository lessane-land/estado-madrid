-- Line health: one row per stretch of trouble on one line, maintained by publish.py after every poll.
-- Run once in the SQL Editor. Then trigger the "episodes-backfill" workflow in GitHub Actions.

create table if not exists public.line_episodes (
  id         bigint generated always as identity primary key,
  line       text not null,            -- graph-app.json line id: metro:L10, cercanias:C5, emt:27
  mode       text not null,
  severity   text not null,            -- Rojo or Amarillo, the worst seen during the episode
  from_stop  text,                     -- incident scope; a new stretch is a new episode
  to_stop    text,
  started    timestamptz not null,
  ended      timestamptz,              -- null while open
  last_seen  timestamptz not null,     -- internal: last status row that carried it
  until      timestamptz,              -- internal: the notice's own expiry, if it stated one
  source     text,
  title      text
);

create index if not exists line_episodes_line_started on public.line_episodes (line, started desc);
create index if not exists line_episodes_open on public.line_episodes (ended) where ended is null;

alter table public.line_episodes enable row level security;

drop policy if exists "anyone can read line_episodes" on public.line_episodes;
create policy "anyone can read line_episodes"
  on public.line_episodes for select
  using (true);

-- Writes come from publish.py with the service key.
