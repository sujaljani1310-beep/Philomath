-- FUTURE disposable local database test; NEVER executed by tests.run_offline.
-- Requires approved canonical fixture and database-execution approval.
BEGIN;
DO $blocked$
BEGIN
    RAISE EXCEPTION 'PostgreSQL scaffold blocked: fixture and execution approval required';
END
$blocked$;

-- Effective privileges include PUBLIC and inherited grants. Column privileges
-- must be checked independently; absence of table grants alone is insufficient.
DO $contract$
DECLARE r text; t text; p text;
BEGIN
    FOREACH r IN ARRAY ARRAY['anon', 'authenticated'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r AND (rolsuper OR rolbypassrls)) THEN
            RAISE EXCEPTION 'Browser role must not bypass RLS';
        END IF;
        FOREACH t IN ARRAY ARRAY['public.conversations', 'public.messages', 'public.user_ai_integrations'] LOOP
            FOREACH p IN ARRAY ARRAY['INSERT', 'UPDATE', 'DELETE', 'TRUNCATE', 'REFERENCES', 'TRIGGER'] LOOP
                IF has_table_privilege(r, t, p) THEN RAISE EXCEPTION 'Unexpected table privilege'; END IF;
            END LOOP;
            FOREACH p IN ARRAY ARRAY['INSERT', 'UPDATE', 'REFERENCES'] LOOP
                IF has_any_column_privilege(r, t, p) THEN RAISE EXCEPTION 'Unexpected column privilege'; END IF;
            END LOOP;
        END LOOP;
        IF has_any_column_privilege(r, 'public.user_ai_integrations', 'SELECT') THEN
            RAISE EXCEPTION 'Browser integration reads must be denied';
        END IF;
    END LOOP;
    FOREACH t IN ARRAY ARRAY['public.conversations', 'public.messages'] LOOP
        IF NOT has_table_privilege('authenticated', t, 'SELECT') THEN
            RAISE EXCEPTION 'Own-row authenticated reads must remain available';
        END IF;
    END LOOP;
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relname IN ('conversations', 'messages', 'user_ai_integrations')
        AND NOT c.relrowsecurity) THEN RAISE EXCEPTION 'RLS must remain enabled'; END IF;
END
$contract$;

-- Capture/compare full pg_get_constraintdef, pg_get_indexdef and
-- pg_get_triggerdef against the approved fixture: do not guess FK names/actions.
-- Also inspect pg_policies, default grants, exposed views and security-definer
-- functions; the table checks above are necessary, not a complete proof.
ROLLBACK;
