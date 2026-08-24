-- Philomath: Google-authenticated users + per-user BYOK AI integrations.
-- Run this once in the Supabase SQL Editor for the same project Philomath uses.

-- Existing conversation tables gain ownership columns. Old rows remain NULL and
-- are not visible to newly authenticated users; new Philomath rows always set them.
alter table public.conversations
    add column if not exists user_id uuid references auth.users(id) on delete cascade;

alter table public.messages
    add column if not exists user_id uuid references auth.users(id) on delete cascade;

create index if not exists conversations_user_id_idx
    on public.conversations(user_id);

create index if not exists messages_user_id_idx
    on public.messages(user_id);

-- API keys are encrypted by FastAPI before they reach this table.
create table if not exists public.user_ai_integrations (
    id uuid primary key default gen_random_uuid(),
    user_id uuid not null references auth.users(id) on delete cascade,
    provider text not null,
    display_name text not null,
    encrypted_api_key text not null,
    api_key_last4 text not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint user_ai_integrations_user_provider_key unique (user_id, provider),
    constraint user_ai_integrations_provider_check
        check (provider in ('openrouter', 'cerebras', 'nvidia', 'gemini', 'grok'))
);

create index if not exists user_ai_integrations_user_id_idx
    on public.user_ai_integrations(user_id);

-- Automatically refresh updated_at when a saved key is replaced.
create or replace function public.set_philomath_updated_at()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists user_ai_integrations_set_updated_at
    on public.user_ai_integrations;

create trigger user_ai_integrations_set_updated_at
before update on public.user_ai_integrations
for each row execute function public.set_philomath_updated_at();

-- Defense in depth. FastAPI uses the server-side service role/secret key and
-- additionally filters every query by user_id. Browser users cannot read the
-- encrypted-key table directly.
alter table public.conversations enable row level security;
alter table public.messages enable row level security;
alter table public.user_ai_integrations enable row level security;

-- Conversations: a signed-in user may only access their own rows.
drop policy if exists "philomath conversations select own" on public.conversations;
create policy "philomath conversations select own"
on public.conversations for select to authenticated
using ((select auth.uid()) = user_id);

drop policy if exists "philomath conversations insert own" on public.conversations;
create policy "philomath conversations insert own"
on public.conversations for insert to authenticated
with check ((select auth.uid()) = user_id);

drop policy if exists "philomath conversations update own" on public.conversations;
create policy "philomath conversations update own"
on public.conversations for update to authenticated
using ((select auth.uid()) = user_id)
with check ((select auth.uid()) = user_id);

drop policy if exists "philomath conversations delete own" on public.conversations;
create policy "philomath conversations delete own"
on public.conversations for delete to authenticated
using ((select auth.uid()) = user_id);

-- Messages: ownership is explicit as well.
drop policy if exists "philomath messages select own" on public.messages;
create policy "philomath messages select own"
on public.messages for select to authenticated
using ((select auth.uid()) = user_id);

drop policy if exists "philomath messages insert own" on public.messages;
create policy "philomath messages insert own"
on public.messages for insert to authenticated
with check ((select auth.uid()) = user_id);

drop policy if exists "philomath messages delete own" on public.messages;
create policy "philomath messages delete own"
on public.messages for delete to authenticated
using ((select auth.uid()) = user_id);

-- Integration records are intentionally backend-only. No anon/authenticated
-- grants are needed because the browser talks to FastAPI, never this table.
revoke all on table public.user_ai_integrations from anon, authenticated;
