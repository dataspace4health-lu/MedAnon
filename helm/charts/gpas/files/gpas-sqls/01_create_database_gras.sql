-- =========================================================================
-- gRAS authentication schema — PostgreSQL 16
--
-- Translated from MySQL 8.0 original (services/gpas/sqls/01_create_database_gras.sql).
-- gRAS provides form-based web UI auth for gPAS.
-- When TTP_GPAS_WEB_AUTH_MODE=gras, WildFly reads credentials from these
-- views via a JDBC security realm.
--
-- All tables live in the "gras" schema within the gpas database.
-- =========================================================================

CREATE SCHEMA IF NOT EXISTS gras;
SET search_path TO gras;

-- ── Tables (created in FK-safe order) ──────────────────────────────────────

CREATE TABLE IF NOT EXISTS domain (
    name         VARCHAR(255) NOT NULL DEFAULT '',
    description  VARCHAR(255) NOT NULL DEFAULT '',
    PRIMARY KEY (name)
);

CREATE TABLE IF NOT EXISTS project (
    name         VARCHAR(255) NOT NULL DEFAULT '',
    description  VARCHAR(255) NOT NULL DEFAULT '',
    PRIMARY KEY (name)
);

-- "user" is a PostgreSQL reserved word — must be quoted
CREATE TABLE IF NOT EXISTS "user" (
    name         VARCHAR(255) NOT NULL DEFAULT '',
    password     VARCHAR(255) DEFAULT NULL,
    active       SMALLINT DEFAULT 1,
    description  VARCHAR(255) NOT NULL DEFAULT '',
    email        VARCHAR(255) NOT NULL DEFAULT '',
    PRIMARY KEY (name)
);

CREATE TABLE IF NOT EXISTS role (
    id            SERIAL PRIMARY KEY,
    name          VARCHAR(255) NOT NULL DEFAULT '',
    project_name  VARCHAR(255) NOT NULL DEFAULT '',
    description   VARCHAR(255) NOT NULL DEFAULT '',
    CONSTRAINT "UNIQUE_NAME_PROJECT_ROLE" UNIQUE (name),
    CONSTRAINT "FK_PROJECT_ROLE" FOREIGN KEY (project_name) REFERENCES project (name)
);

CREATE TABLE IF NOT EXISTS group_ (
    id            SERIAL PRIMARY KEY,
    name          VARCHAR(255) NOT NULL DEFAULT '',
    project_name  VARCHAR(255) NOT NULL DEFAULT '',
    description   VARCHAR(255) NOT NULL DEFAULT '',
    CONSTRAINT "UNIQUE_NAME_PROJECT_GROUP" UNIQUE (name, project_name),
    CONSTRAINT "FK_PROJECT_GROUP" FOREIGN KEY (project_name) REFERENCES project (name)
);

CREATE TABLE IF NOT EXISTS group_role_mapping (
    group_id  INT NOT NULL,
    role_id   INT NOT NULL,
    PRIMARY KEY (group_id, role_id),
    CONSTRAINT "FK_GROUP" FOREIGN KEY (group_id) REFERENCES group_ (id) ON DELETE CASCADE,
    CONSTRAINT "FK_ROLE"  FOREIGN KEY (role_id)  REFERENCES role (id)
);

CREATE TABLE IF NOT EXISTS permission (
    group_id     INT NOT NULL,
    user_name    VARCHAR(255) NOT NULL DEFAULT '',
    domain_name  VARCHAR(255) NOT NULL DEFAULT '',
    PRIMARY KEY (group_id, user_name, domain_name),
    CONSTRAINT "FK_PERMISSION_DOMAIN" FOREIGN KEY (domain_name) REFERENCES domain (name),
    CONSTRAINT "FK_PERMISSION_GROUP"  FOREIGN KEY (group_id)    REFERENCES group_ (id),
    CONSTRAINT "FK_PERMISSION_USER"   FOREIGN KEY (user_name)   REFERENCES "user" (name)
);

-- ── History tables ─────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS hist_domain (
    id           SERIAL PRIMARY KEY,
    name         VARCHAR(255) NOT NULL,
    description  VARCHAR(255) NOT NULL,
    "timestamp"  TIMESTAMP NOT NULL,
    action       VARCHAR(255) NOT NULL
);

CREATE TABLE IF NOT EXISTS hist_project (
    id           SERIAL PRIMARY KEY,
    name         VARCHAR(255) NOT NULL,
    description  VARCHAR(255) NOT NULL,
    "timestamp"  TIMESTAMP NOT NULL,
    action       VARCHAR(255) NOT NULL
);

CREATE TABLE IF NOT EXISTS hist_user (
    id           SERIAL PRIMARY KEY,
    name         VARCHAR(255) NOT NULL DEFAULT '',
    password     VARCHAR(255) DEFAULT NULL,
    active       SMALLINT DEFAULT 1,
    description  VARCHAR(255) NOT NULL DEFAULT '',
    email        VARCHAR(255) NOT NULL DEFAULT '',
    "timestamp"  TIMESTAMP NOT NULL,
    action       VARCHAR(255) NOT NULL
);

CREATE TABLE IF NOT EXISTS hist_group_ (
    id            SERIAL PRIMARY KEY,
    group_id      INT NOT NULL,
    name          VARCHAR(255) NOT NULL,
    project_name  VARCHAR(255) NOT NULL,
    description   VARCHAR(255) NOT NULL,
    "timestamp"   TIMESTAMP NOT NULL,
    action        VARCHAR(255) NOT NULL
);

CREATE TABLE IF NOT EXISTS hist_role (
    id            SERIAL PRIMARY KEY,
    role_id       INT NOT NULL,
    name          VARCHAR(255) NOT NULL,
    project_name  VARCHAR(255) NOT NULL,
    description   VARCHAR(255) NOT NULL,
    "timestamp"   TIMESTAMP NOT NULL,
    action        VARCHAR(255) NOT NULL
);

CREATE TABLE IF NOT EXISTS hist_group_role_mapping (
    id         SERIAL PRIMARY KEY,
    group_id   VARCHAR(255) NOT NULL,
    role_id    VARCHAR(255) NOT NULL,
    "timestamp" TIMESTAMP NOT NULL,
    action     VARCHAR(255) NOT NULL
);

CREATE TABLE IF NOT EXISTS hist_permission (
    id           SERIAL PRIMARY KEY,
    group_id     INT NOT NULL,
    user_name    VARCHAR(255) NOT NULL,
    domain_name  VARCHAR(255) NOT NULL,
    "timestamp"  TIMESTAMP NOT NULL,
    action       VARCHAR(255) NOT NULL
);


-- ── Trigger functions ──────────────────────────────────────────────────────
-- PostgreSQL requires a separate function for each trigger body.

-- domain triggers
CREATE OR REPLACE FUNCTION fn_delete_domain() RETURNS trigger AS $$
BEGIN
    INSERT INTO hist_domain(name, description, "timestamp", action)
        VALUES(OLD.name, OLD.description, now(), 'delete');
    DELETE FROM gras.permission WHERE domain_name = OLD.name;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION fn_update_domain() RETURNS trigger AS $$
BEGIN
    INSERT INTO hist_domain(name, description, "timestamp", action)
        VALUES(OLD.name, OLD.description, now(), 'update');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- project triggers
CREATE OR REPLACE FUNCTION fn_delete_project() RETURNS trigger AS $$
BEGIN
    INSERT INTO hist_project(name, description, "timestamp", action)
        VALUES(OLD.name, OLD.description, now(), 'delete');
    DELETE FROM gras.group_ WHERE project_name = OLD.name;
    DELETE FROM gras.role WHERE project_name = OLD.name;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION fn_update_project() RETURNS trigger AS $$
BEGIN
    INSERT INTO hist_project(name, description, "timestamp", action)
        VALUES(OLD.name, OLD.description, now(), 'update');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- user triggers
CREATE OR REPLACE FUNCTION fn_delete_user() RETURNS trigger AS $$
BEGIN
    INSERT INTO hist_user(name, password, active, description, email, "timestamp", action)
        VALUES(OLD.name, OLD.password, OLD.active, OLD.description, OLD.email, now(), 'delete');
    DELETE FROM gras.permission WHERE user_name = OLD.name;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION fn_update_user() RETURNS trigger AS $$
BEGIN
    INSERT INTO hist_user(name, password, active, description, email, "timestamp", action)
        VALUES(OLD.name, OLD.password, OLD.active, OLD.description, OLD.email, now(), 'update');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- group_ triggers
CREATE OR REPLACE FUNCTION fn_delete_group() RETURNS trigger AS $$
BEGIN
    INSERT INTO hist_group_(group_id, name, project_name, description, "timestamp", action)
        VALUES(OLD.id, OLD.name, OLD.project_name, OLD.description, now(), 'delete');
    DELETE FROM gras.group_role_mapping WHERE group_id = OLD.id;
    DELETE FROM gras.permission WHERE group_id = OLD.id;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION fn_update_group() RETURNS trigger AS $$
BEGIN
    INSERT INTO hist_group_(group_id, name, project_name, description, "timestamp", action)
        VALUES(OLD.id, OLD.name, OLD.project_name, OLD.description, now(), 'update');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- role triggers
CREATE OR REPLACE FUNCTION fn_delete_role() RETURNS trigger AS $$
BEGIN
    INSERT INTO hist_role(role_id, name, project_name, description, "timestamp", action)
        VALUES(OLD.id, OLD.name, OLD.project_name, OLD.description, now(), 'delete');
    DELETE FROM gras.group_role_mapping WHERE role_id = OLD.id;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION fn_update_role() RETURNS trigger AS $$
BEGIN
    INSERT INTO hist_role(role_id, name, project_name, description, "timestamp", action)
        VALUES(OLD.id, OLD.name, OLD.project_name, OLD.description, now(), 'update');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- group_role_mapping triggers
CREATE OR REPLACE FUNCTION fn_insert_group_role_mapping() RETURNS trigger AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM role r, group_ g
        WHERE r.id = NEW.role_id AND g.id = NEW.group_id AND g.project_name = r.project_name
    ) THEN
        RAISE EXCEPTION 'Role and Group have to belong to the same Project';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION fn_delete_group_role_mapping() RETURNS trigger AS $$
BEGIN
    INSERT INTO hist_group_role_mapping(group_id, role_id, "timestamp", action)
        VALUES(OLD.group_id, OLD.role_id, now(), 'delete');
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION fn_update_group_role_mapping() RETURNS trigger AS $$
BEGIN
    INSERT INTO hist_group_role_mapping(group_id, role_id, "timestamp", action)
        VALUES(OLD.group_id, OLD.role_id, now(), 'update');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- permission triggers
CREATE OR REPLACE FUNCTION fn_delete_permission() RETURNS trigger AS $$
BEGIN
    INSERT INTO hist_permission(group_id, user_name, domain_name, "timestamp", action)
        VALUES(OLD.group_id, OLD.user_name, OLD.domain_name, now(), 'delete');
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION fn_update_permission() RETURNS trigger AS $$
BEGIN
    INSERT INTO hist_permission(group_id, user_name, domain_name, "timestamp", action)
        VALUES(OLD.group_id, OLD.user_name, OLD.domain_name, now(), 'update');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;


-- ── Create triggers ────────────────────────────────────────────────────────

DROP TRIGGER IF EXISTS "deleteDomain" ON domain;
CREATE TRIGGER "deleteDomain" BEFORE DELETE ON domain FOR EACH ROW EXECUTE FUNCTION fn_delete_domain();

DROP TRIGGER IF EXISTS "updateDomain" ON domain;
CREATE TRIGGER "updateDomain" BEFORE UPDATE ON domain FOR EACH ROW EXECUTE FUNCTION fn_update_domain();

DROP TRIGGER IF EXISTS "deleteProject" ON project;
CREATE TRIGGER "deleteProject" BEFORE DELETE ON project FOR EACH ROW EXECUTE FUNCTION fn_delete_project();

DROP TRIGGER IF EXISTS "updateProject" ON project;
CREATE TRIGGER "updateProject" BEFORE UPDATE ON project FOR EACH ROW EXECUTE FUNCTION fn_update_project();

DROP TRIGGER IF EXISTS "deleteUser" ON "user";
CREATE TRIGGER "deleteUser" BEFORE DELETE ON "user" FOR EACH ROW EXECUTE FUNCTION fn_delete_user();

DROP TRIGGER IF EXISTS "updateUser" ON "user";
CREATE TRIGGER "updateUser" BEFORE UPDATE ON "user" FOR EACH ROW EXECUTE FUNCTION fn_update_user();

DROP TRIGGER IF EXISTS "deleteGroup" ON group_;
CREATE TRIGGER "deleteGroup" BEFORE DELETE ON group_ FOR EACH ROW EXECUTE FUNCTION fn_delete_group();

DROP TRIGGER IF EXISTS "updateGroup" ON group_;
CREATE TRIGGER "updateGroup" BEFORE UPDATE ON group_ FOR EACH ROW EXECUTE FUNCTION fn_update_group();

DROP TRIGGER IF EXISTS "deleteRole" ON role;
CREATE TRIGGER "deleteRole" BEFORE DELETE ON role FOR EACH ROW EXECUTE FUNCTION fn_delete_role();

DROP TRIGGER IF EXISTS "updateRole" ON role;
CREATE TRIGGER "updateRole" BEFORE UPDATE ON role FOR EACH ROW EXECUTE FUNCTION fn_update_role();

DROP TRIGGER IF EXISTS "insertGroupRoleMapping" ON group_role_mapping;
CREATE TRIGGER "insertGroupRoleMapping" BEFORE INSERT ON group_role_mapping FOR EACH ROW EXECUTE FUNCTION fn_insert_group_role_mapping();

DROP TRIGGER IF EXISTS "deleteGroupRoleMapping" ON group_role_mapping;
CREATE TRIGGER "deleteGroupRoleMapping" BEFORE DELETE ON group_role_mapping FOR EACH ROW EXECUTE FUNCTION fn_delete_group_role_mapping();

DROP TRIGGER IF EXISTS "updateGroupRoleMapping" ON group_role_mapping;
CREATE TRIGGER "updateGroupRoleMapping" BEFORE UPDATE ON group_role_mapping FOR EACH ROW EXECUTE FUNCTION fn_update_group_role_mapping();

DROP TRIGGER IF EXISTS "deletePermission" ON permission;
CREATE TRIGGER "deletePermission" BEFORE DELETE ON permission FOR EACH ROW EXECUTE FUNCTION fn_delete_permission();

DROP TRIGGER IF EXISTS "updatePermission" ON permission;
CREATE TRIGGER "updatePermission" BEFORE UPDATE ON permission FOR EACH ROW EXECUTE FUNCTION fn_update_permission();


-- ── Views ──────────────────────────────────────────────────────────────────
-- WildFly's JDBC security realm queries these views for auth.

CREATE OR REPLACE VIEW domainuser_password AS
    SELECT DISTINCT
        p.user_name || '@' || p.domain_name AS domainuser,
        u.password
    FROM permission p
    JOIN "user" u ON u.name = p.user_name AND u.active = 1;

CREATE OR REPLACE VIEW domainuser_role AS
    SELECT DISTINCT
        p.user_name || '@' || p.domain_name AS domainuser,
        r.name AS role
    FROM permission p
    JOIN group_role_mapping m ON p.group_id = m.group_id
    JOIN role r ON m.role_id = r.id
    JOIN "user" u ON u.name = p.user_name AND u.active = 1;

CREATE OR REPLACE VIEW validate AS
    SELECT DISTINCT
        a.domainuser,
        a.password,
        b.role
    FROM domainuser_password a
    JOIN domainuser_role b ON a.domainuser = b.domainuser;


-- ── Comfort procedures (as PostgreSQL functions) ───────────────────────────

CREATE OR REPLACE FUNCTION "createDomain"(
    domainName VARCHAR(255),
    p_description VARCHAR(255)
) RETURNS void AS $$
BEGIN
    INSERT INTO gras.domain (name, description) VALUES (domainName, p_description)
    ON CONFLICT DO NOTHING;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION "createProject"(
    projectName VARCHAR(255),
    p_description VARCHAR(255)
) RETURNS void AS $$
BEGIN
    INSERT INTO gras.project (name, description) VALUES (projectName, p_description)
    ON CONFLICT DO NOTHING;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION "disableUser"(
    userName VARCHAR(255)
) RETURNS void AS $$
BEGIN
    UPDATE gras."user" SET active = 0 WHERE name = userName;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION "enableUser"(
    userName VARCHAR(255)
) RETURNS void AS $$
BEGIN
    UPDATE gras."user" SET active = 1 WHERE name = userName;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION "changePassword"(
    userName VARCHAR(255),
    newPassword VARCHAR(255)
) RETURNS void AS $$
BEGIN
    UPDATE gras."user" SET password = encode(public.digest(newPassword, 'sha256'), 'hex')
    WHERE name = userName;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION "createUser"(
    userName VARCHAR(255),
    p_password VARCHAR(255),
    p_description VARCHAR(255)
) RETURNS void AS $$
BEGIN
    INSERT INTO gras."user" (name, password, active, description)
    VALUES (userName, encode(public.digest(p_password, 'sha256'), 'hex'), 1, p_description)
    ON CONFLICT DO NOTHING;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION "createGroup"(
    projectName VARCHAR(255),
    groupName VARCHAR(255),
    p_description VARCHAR(255)
) RETURNS void AS $$
BEGIN
    INSERT INTO gras.group_ (name, project_name, description)
    VALUES (groupName, projectName, p_description)
    ON CONFLICT (name, project_name) DO NOTHING;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION "createRole"(
    projectName VARCHAR(255),
    roleName VARCHAR(255),
    p_description VARCHAR(255)
) RETURNS void AS $$
BEGIN
    INSERT INTO gras.role (name, project_name, description)
    VALUES (roleName, projectName, p_description)
    ON CONFLICT ON CONSTRAINT "UNIQUE_NAME_PROJECT_ROLE" DO NOTHING;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION "createGroupRoleMapping"(
    projectName VARCHAR(255),
    groupName VARCHAR(255),
    roleName VARCHAR(255)
) RETURNS void AS $$
BEGIN
    INSERT INTO gras.group_role_mapping (group_id, role_id) VALUES (
        (SELECT id FROM gras.group_ WHERE name = groupName AND project_name = projectName),
        (SELECT id FROM gras.role WHERE name = roleName)
    )
    ON CONFLICT DO NOTHING;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION "grantAdminRights"(
    domainName VARCHAR(255),
    projectName VARCHAR(255),
    userName VARCHAR(255)
) RETURNS void AS $$
BEGIN
    INSERT INTO gras.permission (group_id, user_name, domain_name)
    SELECT DISTINCT id, userName, domainName
    FROM gras.group_ WHERE project_name = projectName
    ON CONFLICT DO NOTHING;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION "grantStandardRights"(
    domainName VARCHAR(255),
    projectName VARCHAR(255),
    userName VARCHAR(255)
) RETURNS void AS $$
BEGIN
    DELETE FROM gras.permission p
    USING gras.group_ g
    WHERE p.group_id = g.id
      AND p.user_name = userName
      AND p.domain_name = domainName
      AND g.project_name = projectName;

    INSERT INTO gras.permission (group_id, user_name, domain_name)
    SELECT DISTINCT id, userName, domainName
    FROM gras.group_
    WHERE project_name = projectName AND name !~* 'admin';
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION "testCredetials"(
    userName VARCHAR(255),
    passwd VARCHAR(255)
) RETURNS TABLE(roles VARCHAR(255)) AS $$
BEGIN
    RETURN QUERY
    SELECT DISTINCT r.name AS roles
    FROM gras.permission p
    JOIN gras.group_role_mapping m ON p.group_id = m.group_id
    JOIN gras.role r ON m.role_id = r.id
    JOIN gras."user" u ON u.name = p.user_name AND u.active = 1
    WHERE p.user_name || '@' || p.domain_name = userName
      AND u.password = encode(public.digest(passwd, 'sha256'), 'hex');
END;
$$ LANGUAGE plpgsql;


-- ── Database role ──────────────────────────────────────────────────────────

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'gras_user') THEN
        CREATE ROLE gras_user LOGIN PASSWORD 'gras_password';
    END IF;
END
$$;
GRANT USAGE ON SCHEMA gras TO gras_user;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA gras TO gras_user;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA gras TO gras_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA gras GRANT ALL ON TABLES TO gras_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA gras GRANT ALL ON SEQUENCES TO gras_user;

-- Reset search_path
SET search_path TO public;
