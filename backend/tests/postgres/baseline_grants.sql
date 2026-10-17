-- Reproduces the 70 known table-grant rows. Unknown inherited/PUBLIC/default
-- properties are NOT inferred. This is a known-properties baseline, not a full
-- copy of the production security context. Run only on an empty staging fixture.
GRANT ALL PRIVILEGES ON TABLE public.conversations, public.messages
    TO anon, authenticated, service_role;
GRANT ALL PRIVILEGES ON TABLE public.user_ai_integrations TO service_role;
