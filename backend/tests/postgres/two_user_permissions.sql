-- FUTURE psql scaffold. Requires canonical fixture with synthetic Auth users,
-- variables user_a/user_b containing their UUIDs, and a privileged setup role.
-- Not executed, loaded or connected by the offline Python test suite.
BEGIN;
DO $blocked$
BEGIN
    RAISE EXCEPTION 'Two-user scaffold blocked: canonical fixture and execution approval required';
END
$blocked$;

-- Columns/defaults/extra NOT NULL fields must be checked before finalizing these
-- fixture inserts. They are a test recipe, not a reconstruction of missing DDL.
INSERT INTO public.conversations (id, user_id, title, mode) VALUES
    ('philomath_fixture_A', :'user_a'::uuid, 'Synthetic A', 'basic'),
    ('philomath_fixture_B', :'user_b'::uuid, 'Synthetic B', 'basic');
INSERT INTO public.messages (id, conversation_id, user_id, role, content) VALUES
    ('11111111-1111-4111-8111-111111111111', 'philomath_fixture_A', :'user_a'::uuid, 'user', 'Synthetic A'),
    ('22222222-2222-4222-8222-222222222222', 'philomath_fixture_B', :'user_b'::uuid, 'user', 'Synthetic B');

SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claim.sub', :'user_a', true);
SELECT set_config('request.jwt.claims', json_build_object('sub', :'user_a', 'role', 'authenticated')::text, true);
DO $isolation$
DECLARE denied boolean;
BEGIN
    IF (SELECT count(*) FROM public.conversations WHERE id = 'philomath_fixture_A') <> 1
       OR EXISTS (SELECT 1 FROM public.conversations WHERE id = 'philomath_fixture_B')
       OR EXISTS (SELECT 1 FROM public.messages WHERE conversation_id = 'philomath_fixture_B') THEN
        RAISE EXCEPTION 'Two-user read isolation failed';
    END IF;
    denied := false;
    BEGIN UPDATE public.conversations SET title = 'forbidden' WHERE id = 'philomath_fixture_A';
    EXCEPTION WHEN insufficient_privilege THEN denied := true; END;
    IF NOT denied THEN RAISE EXCEPTION 'Direct own-row UPDATE was not denied'; END IF;
    denied := false;
    BEGIN DELETE FROM public.messages WHERE conversation_id = 'philomath_fixture_A';
    EXCEPTION WHEN insufficient_privilege THEN denied := true; END;
    IF NOT denied THEN RAISE EXCEPTION 'Direct own-row DELETE was not denied'; END IF;
    denied := false;
    BEGIN PERFORM 1 FROM public.user_ai_integrations LIMIT 1;
    EXCEPTION WHEN insufficient_privilege THEN denied := true; END;
    IF NOT denied THEN RAISE EXCEPTION 'Browser integration read was not denied'; END IF;
END
$isolation$;
RESET ROLE;
-- Complete the symmetric B, anon, direct INSERT and privileged negative-owner
-- cases only after fixture metadata is reviewed. Verify row postconditions as
-- well as denial status. Use separate sessions for genuine concurrency tests.
ROLLBACK;
