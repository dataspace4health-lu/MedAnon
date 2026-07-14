-- =========================================================================
-- gRAS seed data for gPAS  PostgreSQL 16
--
-- Translated from MySQL 8.0 original (services/gpas/sqls/02_init_database_gras_for_gpas.sql).
-- Replaces MySQL @variables and CALL statements with PostgreSQL function calls.
--
-- NOTE: gRAS stores users as username@domain in the domainuser_password view
-- (see the views in 01_create_database_gras.sql).
-- When logging into the gPAS web UI or using Basic auth for the FHIR API,
-- you MUST use the domain-qualified form:
--   admin@ths / ttp-tools  (admin privileges)
--   user@ths  / ttp-tools  (standard privileges)
-- =========================================================================

SET search_path TO gras;

-- Use a DO block to hold local variables (replaces MySQL @variables)
DO $$
DECLARE
    v_domainName  VARCHAR(255) := 'ths';
    v_projectName VARCHAR(255) := 'gpas';
    v_displayName VARCHAR(255) := 'gPAS';
BEGIN
    -- domain
    PERFORM gras."createDomain"(v_domainName, 'parent domain');

    -- project
    PERFORM gras."createProject"(v_projectName, v_displayName);

    -- groups
    PERFORM gras."createGroup"(v_projectName, v_displayName || '-users', 'this group is for users with basic right');
    PERFORM gras."createGroup"(v_projectName, v_displayName || '-admins', 'this group is for users with extended right');

    -- roles
    PERFORM gras."createRole"(v_projectName, 'role.' || v_projectName || '.user', v_displayName || ' userspace');
    PERFORM gras."createRole"(v_projectName, 'role.' || v_projectName || '.admin', v_displayName || ' adminspace');

    -- group-role mappings
    PERFORM gras."createGroupRoleMapping"(v_projectName, v_displayName || '-users', 'role.' || v_projectName || '.user');
    PERFORM gras."createGroupRoleMapping"(v_projectName, v_displayName || '-admins', 'role.' || v_projectName || '.user');
    PERFORM gras."createGroupRoleMapping"(v_projectName, v_displayName || '-admins', 'role.' || v_projectName || '.admin');

    -- default users
    PERFORM gras."createUser"('admin', 'ttp-tools', 'user for admin privileges');
    PERFORM gras."createUser"('user', 'ttp-tools', 'user for standard privileges');

    -- grant privileges
    PERFORM gras."grantAdminRights"(v_domainName, v_projectName, 'admin');
    PERFORM gras."grantStandardRights"(v_domainName, v_projectName, 'user');
END
$$;

SET search_path TO public;
