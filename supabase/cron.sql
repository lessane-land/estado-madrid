-- Supabase as the clock. GitHub's own schedule fired three times in eleven hours on day one,
-- so Postgres asks GitHub to run the workflow every five minutes instead.
--
-- Before running this:
--   1. GitHub: Settings, Developer settings, Personal access tokens, Fine-grained tokens, Generate.
--      Repository access: only lessane-land/estado-madrid. Permissions: Actions, Read and write.
--      Copy the token (starts with github_pat_).
--   2. Supabase: Database, Extensions: enable pg_cron and pg_net (or run the two lines below).
--   3. Paste the token into the vault line, run this file once in the SQL Editor, then delete the
--      token from your clipboard. The token lives only in the Supabase Vault from then on.

create extension if not exists pg_cron;
create extension if not exists pg_net;

-- Store the token once. To rotate it later: select vault.update_secret(id, 'new token') from vault.secrets where name = 'github_dispatch_token';
select vault.create_secret('PASTE_GITHUB_TOKEN_HERE', 'github_dispatch_token', 'Fine-grained PAT, Actions write on estado-madrid');

-- Ask GitHub to start the "estado" workflow on main every five minutes.
select cron.schedule(
  'estado-madrid-dispatch',
  '*/5 * * * *',
  $$
  select net.http_post(
    url     := 'https://api.github.com/repos/lessane-land/estado-madrid/actions/workflows/estado.yml/dispatches',
    headers := jsonb_build_object(
      'Authorization', 'Bearer ' || (select decrypted_secret from vault.decrypted_secrets where name = 'github_dispatch_token'),
      'Accept',        'application/vnd.github+json',
      'User-Agent',    'estado-madrid-cron',
      'Content-Type',  'application/json'),
    body    := '{"ref":"main"}'::jsonb
  );
  $$
);

-- Let the app read the history too (the live row already has this policy).
create policy "anyone can read estado_history"
  on public.estado_history for select
  using (true);

-- Useful checks afterwards:
--   select jobname, schedule, active from cron.job;
--   select status, return_message, start_time from cron.job_run_details order by start_time desc limit 10;
--   select id, status_code, error_msg from net._http_response order by id desc limit 5;   -- 204 means GitHub accepted
-- To stop it: select cron.unschedule('estado-madrid-dispatch');
