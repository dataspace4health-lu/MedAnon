-- PostgreSQL port of the gRAS seed data for gPAS: default domain/project/
-- groups/roles plus an admin and a standard user, so the web UI has at
-- least one account to log in with immediately after first boot.
--
-- MySQL used session variables (SET @var) to avoid repeating literals;
-- Postgres SQL scripts have no equivalent, so the values are inlined
-- directly. Keep 'ths' / 'gpas' / 'gPAS' in sync if ever changed.

-- "public" stays on the path so the createUser/changePassword procedures can
-- still reach pgcrypto's digest() (installed in public by 01_create_database_gras.sql).
SET search_path TO gras, public;

-- domain
CALL createDomain('ths', 'parent domain');

-- project
CALL createProject('gpas', 'gPAS');

-- group_
CALL createGroup('gpas', 'gPAS-users', 'this group is for users with basic right');
CALL createGroup('gpas', 'gPAS-admins', 'this group is for users with extended right');

-- role
CALL createRole('gpas', 'role.gpas.user', 'gPAS userspace');
CALL createRole('gpas', 'role.gpas.admin', 'gPAS adminspace');

-- group_role_mapping
CALL createGroupRoleMapping('gpas', 'gPAS-users', 'role.gpas.user');
CALL createGroupRoleMapping('gpas', 'gPAS-admins', 'role.gpas.user');
CALL createGroupRoleMapping('gpas', 'gPAS-admins', 'role.gpas.admin');

-- default users
CALL createUser('admin', 'ttp-tools', 'user for admin privileges');
CALL createUser('user', 'ttp-tools', 'user for standard privileges');

-- grant privileges for project
CALL grantAdminRights('ths', 'gpas', 'admin');
CALL grantStandardRights('ths', 'gpas', 'user');

RESET search_path;
