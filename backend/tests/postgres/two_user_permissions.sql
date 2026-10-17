-- Invoked by hosted.py inside a guarded, force-rollback transaction. The IDs
-- belong only to confirmed synthetic users/fixtures created by that invocation.
DO $two_users$
DECLARE
    users uuid[] := ARRAY[current_setting('philomath.batch4_user_a')::uuid,
                          current_setting('philomath.batch4_user_b')::uuid];
    parents text[] := ARRAY[current_setting('philomath.batch4_parent_a'),
                            current_setting('philomath.batch4_parent_b')];
    i integer; denied boolean; command text; role_name text; table_name text;
BEGIN
    FOR i IN 1..2 LOOP
        PERFORM set_config('request.jwt.claim.sub',users[i]::text,true);
        PERFORM set_config('request.jwt.claims',json_build_object('sub',users[i],'role','authenticated')::text,true);
        SET LOCAL ROLE authenticated;
        IF (SELECT count(*) FROM public.conversations WHERE id=parents[i])<>1
           OR EXISTS (SELECT 1 FROM public.conversations WHERE id=parents[3-i])
           OR (SELECT count(*) FROM public.messages WHERE conversation_id=parents[i])<>1
           OR EXISTS (SELECT 1 FROM public.messages WHERE conversation_id=parents[3-i]) THEN
            RAISE EXCEPTION 'Direct SQL two-user isolation failed';
        END IF;
        FOREACH command IN ARRAY ARRAY[
            format('UPDATE public.conversations SET title=%L WHERE id=%L','forbidden',parents[i]),
            format('DELETE FROM public.conversations WHERE id=%L',parents[i]),
            'INSERT INTO public.conversations DEFAULT VALUES',
            format('UPDATE public.messages SET content=%L WHERE conversation_id=%L','forbidden',parents[i]),
            format('DELETE FROM public.messages WHERE conversation_id=%L',parents[i]),
            'INSERT INTO public.messages DEFAULT VALUES',
            'SELECT id FROM public.user_ai_integrations LIMIT 0'] LOOP
            denied:=false;
            BEGIN EXECUTE command;
            EXCEPTION WHEN insufficient_privilege THEN denied:=true; END;
            IF NOT denied THEN RAISE EXCEPTION 'Direct SQL browser operation unexpectedly allowed'; END IF;
        END LOOP;
        RESET ROLE;
    END LOOP;
    SET LOCAL ROLE anon;
    FOREACH table_name IN ARRAY ARRAY['conversations','messages','user_ai_integrations'] LOOP
        denied:=false;
        BEGIN EXECUTE format('SELECT id FROM public.%I LIMIT 0',table_name);
        EXCEPTION WHEN insufficient_privilege THEN denied:=true; END;
        IF NOT denied THEN RAISE EXCEPTION 'Anonymous SQL read unexpectedly allowed'; END IF;
    END LOOP;
    RESET ROLE;
    -- Check dangerous grants without executing a destructive TRUNCATE.
    FOREACH role_name IN ARRAY ARRAY['anon','authenticated'] LOOP
        FOREACH table_name IN ARRAY ARRAY['public.conversations','public.messages','public.user_ai_integrations'] LOOP
            IF has_table_privilege(role_name,table_name,'TRUNCATE')
               OR has_table_privilege(role_name,table_name,'TRIGGER')
               OR has_table_privilege(role_name,table_name,'REFERENCES') THEN
                RAISE EXCEPTION 'Dangerous browser privilege remains';
            END IF;
        END LOOP;
    END LOOP;
END
$two_users$;
