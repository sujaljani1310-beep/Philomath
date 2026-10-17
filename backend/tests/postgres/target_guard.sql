-- Execution acknowledgement, NOT independent project-identity proof.
-- The runner must verify the DSN, TLS and staging API before setting this GUC.
DO $target$
DECLARE target text := current_setting('philomath.batch4_target', true);
BEGIN
    IF current_user <> 'postgres' THEN
        RAISE EXCEPTION 'Batch 4 requires the reviewed administrative role';
    END IF;
    IF target = 'offline-local' AND current_database() = 'philomath_batch4_test'
       AND inet_client_addr() IS NULL THEN
        RETURN;
    END IF;
    IF target IS DISTINCT FROM 'tdiqzmaqvoynspmilwra'
       OR current_database() <> 'postgres' THEN
        RAISE EXCEPTION 'Batch 4 target is not explicitly verified staging';
    END IF;
END
$target$;
SET LOCAL lock_timeout = '2s';
SET LOCAL statement_timeout = '20s';
