-- Read-only catalog assertions; candidate execution aborts on unknown state.
DO $catalog$
DECLARE t text; r text;
BEGIN
    IF current_setting('session_replication_role') <> 'origin' THEN
        RAISE EXCEPTION 'Ordinary constraint enforcement is required';
    END IF;
    FOREACH t IN ARRAY ARRAY['conversations','messages','user_ai_integrations'] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname='public' AND c.relname=t AND c.relkind='r'
            AND c.relrowsecurity AND NOT c.relforcerowsecurity
            AND pg_get_userbyid(c.relowner)='postgres') THEN
            RAISE EXCEPTION 'Unexpected table ownership/type/RLS state';
        END IF;
    END LOOP;
    IF (SELECT count(*) FROM pg_constraint WHERE conrelid IN
        ('public.conversations'::regclass,'public.messages'::regclass,
         'public.user_ai_integrations'::regclass)) <> 9
       OR EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid IN
        ('public.conversations'::regclass,'public.messages'::regclass,
         'public.user_ai_integrations'::regclass)
         AND (NOT convalidated OR condeferrable OR condeferred)) THEN
        RAISE EXCEPTION 'Unexpected baseline constraints or validation state';
    END IF;
    IF (SELECT count(*) FROM pg_constraint WHERE conrelid IN
        ('public.conversations'::regclass,'public.messages'::regclass,
         'public.user_ai_integrations'::regclass) AND contype='f') <> 4
       OR EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid IN
        ('public.conversations'::regclass,'public.messages'::regclass,
         'public.user_ai_integrations'::regclass) AND contype='f'
         AND (confdeltype<>'c' OR confupdtype<>'a' OR confmatchtype<>'s')) THEN
        RAISE EXCEPTION 'Unknown FK actions';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_index WHERE indrelid IN
        ('public.conversations'::regclass,'public.messages'::regclass,
         'public.user_ai_integrations'::regclass) AND (NOT indisvalid OR NOT indisready)) THEN
        RAISE EXCEPTION 'Invalid baseline index';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_trigger t JOIN pg_constraint k ON k.oid=t.tgconstraint
        WHERE k.conrelid IN ('public.conversations'::regclass,'public.messages'::regclass,
        'public.user_ai_integrations'::regclass) AND t.tgisinternal AND t.tgenabled NOT IN ('O','A')) THEN
        RAISE EXCEPTION 'Foreign-key enforcement trigger is disabled';
    END IF;
    FOREACH r IN ARRAY ARRAY['anon','authenticated'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname=r AND (rolsuper OR rolbypassrls))
           OR EXISTS (SELECT 1 FROM pg_auth_members m JOIN pg_roles member ON member.oid=m.member WHERE member.rolname=r) THEN
            RAISE EXCEPTION 'Browser role inherits privileged capabilities';
        END IF;
    END LOOP;
    IF EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname='public' AND p.prosecdef AND
        (has_function_privilege('anon',p.oid,'EXECUTE') OR
         has_function_privilege('authenticated',p.oid,'EXECUTE'))) THEN
        RAISE EXCEPTION 'Browser-callable SECURITY DEFINER function needs review';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname='public' AND p.proname='set_philomath_updated_at'
        AND pg_get_userbyid(p.proowner)<>'postgres') THEN
        RAISE EXCEPTION 'Timestamp function ownership needs separate review';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public' AND c.relkind IN ('v','m','f') AND
        (has_table_privilege('anon',c.oid,'SELECT') OR
         has_table_privilege('authenticated',c.oid,'SELECT'))) THEN
        RAISE EXCEPTION 'Browser-readable view/foreign table needs review';
    END IF;
END
$catalog$;
