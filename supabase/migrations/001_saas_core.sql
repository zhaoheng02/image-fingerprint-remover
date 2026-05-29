create extension if not exists pgcrypto;

create table if not exists public.profiles (
  user_id uuid primary key references auth.users(id) on delete cascade,
  email text,
  credits integer not null default 0 check (credits >= 0),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.usage_events (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  job_id text,
  event_type text not null,
  credits_delta integer not null,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create table if not exists public.orders (
  id uuid primary key default gen_random_uuid(),
  provider text not null,
  provider_order_id text not null,
  user_id uuid not null references auth.users(id) on delete cascade,
  credits integer not null check (credits > 0),
  amount_cents integer,
  currency text,
  status text not null,
  raw_payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  unique (provider, provider_order_id)
);

create table if not exists public.payment_webhook_events (
  id uuid primary key default gen_random_uuid(),
  provider text not null,
  event_id text,
  event_name text not null,
  raw_payload jsonb not null,
  created_at timestamptz not null default now(),
  unique (provider, event_id)
);

alter table public.profiles enable row level security;
alter table public.usage_events enable row level security;
alter table public.orders enable row level security;
alter table public.payment_webhook_events enable row level security;

drop policy if exists "Users can read own profile" on public.profiles;
create policy "Users can read own profile"
on public.profiles for select
using (auth.uid() = user_id);

drop policy if exists "Users can read own usage" on public.usage_events;
create policy "Users can read own usage"
on public.usage_events for select
using (auth.uid() = user_id);

drop policy if exists "Users can read own orders" on public.orders;
create policy "Users can read own orders"
on public.orders for select
using (auth.uid() = user_id);

create or replace function public.touch_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists profiles_touch_updated_at on public.profiles;
create trigger profiles_touch_updated_at
before update on public.profiles
for each row execute function public.touch_updated_at();

create or replace function public.consume_credit(
  p_user_id uuid,
  p_job_id text,
  p_metadata jsonb default '{}'::jsonb
)
returns integer
language plpgsql
security definer
set search_path = public
as $$
declare
  v_remaining integer;
begin
  update public.profiles
  set credits = credits - 1
  where user_id = p_user_id and credits > 0
  returning credits into v_remaining;

  if v_remaining is null then
    raise exception 'Insufficient credits'
      using errcode = 'P0001',
      detail = '402';
  end if;

  insert into public.usage_events (user_id, job_id, event_type, credits_delta, metadata)
  values (p_user_id, p_job_id, 'clean_image', -1, coalesce(p_metadata, '{}'::jsonb));

  return v_remaining;
end;
$$;

create or replace function public.grant_credits(
  p_user_id uuid,
  p_delta integer,
  p_reason text,
  p_metadata jsonb default '{}'::jsonb
)
returns integer
language plpgsql
security definer
set search_path = public
as $$
declare
  v_remaining integer;
begin
  if p_delta <= 0 then
    raise exception 'Credit grant must be positive';
  end if;

  insert into public.profiles (user_id, credits)
  values (p_user_id, p_delta)
  on conflict (user_id)
  do update set credits = public.profiles.credits + excluded.credits
  returning credits into v_remaining;

  insert into public.usage_events (user_id, event_type, credits_delta, metadata)
  values (p_user_id, coalesce(p_reason, 'credit_grant'), p_delta, coalesce(p_metadata, '{}'::jsonb));

  return v_remaining;
end;
$$;

grant execute on function public.consume_credit(uuid, text, jsonb) to service_role;
grant execute on function public.grant_credits(uuid, integer, text, jsonb) to service_role;
