-- LOCAL MODEL ONLY: not Supabase Auth, never execute in a hosted project.
DO $local$
BEGIN
    IF current_database() <> 'philomath_batch4_test' OR inet_client_addr() IS NOT NULL THEN
        RAISE EXCEPTION 'Local fixture only';
    END IF;
END
$local$;
CREATE ROLE anon NOLOGIN;
CREATE ROLE authenticated NOLOGIN;
CREATE ROLE service_role NOLOGIN BYPASSRLS;
CREATE ROLE synthetic_auth_admin NOLOGIN;
CREATE SCHEMA auth;
CREATE TABLE auth.users (id uuid PRIMARY KEY);
GRANT USAGE ON SCHEMA auth TO anon, authenticated, service_role, synthetic_auth_admin;
GRANT DELETE, SELECT ON auth.users TO synthetic_auth_admin;
CREATE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql STABLE
    SET search_path = '' AS $$
    SELECT coalesce(nullif(current_setting('request.jwt.claim.sub', true), ''),
        nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub')::uuid
$$;
GRANT EXECUTE ON FUNCTION auth.uid() TO anon, authenticated, service_role;
GRANT USAGE ON SCHEMA public TO anon, authenticated, service_role;
