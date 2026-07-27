-- PostgreSQL port of the gPAS core pseudonymization schema (public schema,
-- database "gpas" - already created by the postgres image via POSTGRES_DB).
--
-- Why this file exists at all: gPAS's own EclipseLink/JPA layer is fully
-- capable of auto-creating this schema on first boot (TTP_GPAS_DB_DBMS=postgresql
-- makes it emit valid Postgres DDL). But EclipseLink's schema-generation probes
-- each table with "SELECT 1 FROM <table>" before deciding whether to CREATE it.
-- On a genuinely empty database that probe fails (relation does not exist),
-- and unlike MySQL, PostgreSQL aborts the *entire* transaction on any error -
-- every following statement in that transaction (including the CREATE TABLE
-- meant to fix the problem) then fails with "current transaction is aborted".
-- Pre-creating the tables here means the probe always succeeds, so
-- EclipseLink's create-or-extend logic never needs to run DDL against
-- PostgreSQL in the first place.
--
-- Table shapes below are copied verbatim from gPAS 2025.2.0's own
-- EclipseLink-generated DDL (captured from a live boot against this exact
-- Postgres target), not translated from the MySQL dump - this is the
-- authoritative shape the deployed entity classes expect.

CREATE TABLE IF NOT EXISTS domain (
    name VARCHAR(255) NOT NULL,
    alphabet VARCHAR(255),
    comment VARCHAR(255),
    create_timestamp TIMESTAMP NOT NULL DEFAULT now(),
    expiration_properties VARCHAR(255),
    generatorclass VARCHAR(255),
    label VARCHAR(255),
    -- Wider than EclipseLink's own VARCHAR(255) mapping (which is what got
    -- captured into this file originally): gPAS's built-in bootstrap of its
    -- reserved "internal_anonymisation_domain" writes a properties value
    -- longer than 255 chars. MySQL's default non-strict mode silently
    -- truncates that; PostgreSQL always enforces the column length and
    -- rejects the insert outright, permanently failing that bootstrap.
    -- Since this column is pre-created here (JPA's create-or-extend never
    -- narrows an existing column), sizing it generously is a safe fix -
    -- matches the original MySQL dump, which used varchar(1023) for this
    -- same column.
    properties VARCHAR(1023),
    update_timestamp TIMESTAMP NOT NULL DEFAULT now(),
    PRIMARY KEY (name)
);

CREATE TABLE IF NOT EXISTS domain_parents (
    parentdomain VARCHAR(255) NOT NULL,
    domain VARCHAR(255) NOT NULL,
    PRIMARY KEY (parentdomain, domain)
);

CREATE TABLE IF NOT EXISTS psn (
    encoded_expiration_date SMALLINT,
    pseudonym VARCHAR(255),
    originalvalue VARCHAR(255) NOT NULL,
    domain VARCHAR(255) NOT NULL,
    PRIMARY KEY (originalvalue, domain)
);

-- Collision-check / reverse-lookup index (NOT in gPAS's own DDL).
-- $pseudonymizeAllowCreate, when creating a NEW pseudonym, verifies the
-- candidate is unique via  WHERE domain=? AND pseudonym=? . The primary key is
-- (originalvalue, domain), so pseudonym is unindexed and that check was a full
-- Seq Scan of psn on every new value (~19ms at 250k rows, all CPU, degrading
-- linearly). Measured effect of this index: ~47ms/value -> ~1.8ms/value
-- (~24x), turning a ~5.6h full-dataset export into ~13min. Also accelerates
-- $dePseudonymize (reverse lookup by pseudonym), which had the same Seq Scan.
CREATE INDEX IF NOT EXISTS idx_psn_pseudonym_domain ON psn (pseudonym, domain);

CREATE TABLE IF NOT EXISTS mpsn (
    encoded_expiration_date SMALLINT,
    originalvalue VARCHAR(255) NOT NULL,
    pseudonym VARCHAR(255) NOT NULL,
    domain VARCHAR(255) NOT NULL,
    PRIMARY KEY (originalvalue, pseudonym, domain)
);

-- Same collision-check pattern for multi-pseudonym domains: the PK
-- (originalvalue, pseudonym, domain) does not lead with pseudonym, so a
-- WHERE domain=? AND pseudonym=? lookup cannot use it. Cheap insurance so a
-- convert_to_multi_psn_domain domain does not reintroduce the Seq Scan.
CREATE INDEX IF NOT EXISTS idx_mpsn_pseudonym_domain ON mpsn (pseudonym, domain);

CREATE TABLE IF NOT EXISTS stat_entry (
    stat_entry_id BIGINT NOT NULL,
    entrydate TIMESTAMP,
    PRIMARY KEY (stat_entry_id)
);

CREATE TABLE IF NOT EXISTS stat_value (
    stat_value_id BIGINT,
    stat_value BIGINT,
    stat_attr VARCHAR(255)
);

CREATE TABLE IF NOT EXISTS sequence (
    seq_name VARCHAR(50) NOT NULL,
    seq_count DECIMAL(38),
    PRIMARY KEY (seq_name)
);

-- Statistics view (pseudonyms per domain), ported from the MySQL dump.
CREATE OR REPLACE VIEW psn_domain_count AS
    SELECT
        'pseudonyms_per_domain.' || d.name AS attribut,
        (COUNT(p.pseudonym) + COUNT(m.pseudonym)) AS value
    FROM domain d
        LEFT JOIN psn p ON p.domain = d.name
        LEFT JOIN mpsn m ON m.domain = d.name
    WHERE d.name != 'internal_anonymisation_domain'
    GROUP BY d.name;

-- *** admin utility procedures (manual DBA use, not called by the app) ***
-- Ported from MySQL's convert_to_multi_psn_domain / convert_to_single_psn_domain.

CREATE OR REPLACE PROCEDURE convert_to_multi_psn_domain(IN in_domain VARCHAR(255))
LANGUAGE plpgsql
AS $$
DECLARE
    conflict INT;
    msg VARCHAR(255);
BEGIN
    SELECT CASE WHEN COUNT(*) > 0 THEN 0 ELSE 1 END INTO conflict FROM domain WHERE name = in_domain;
    IF conflict = 1 THEN
        msg := 'Domain "' || in_domain || '" not exists.';
        RAISE EXCEPTION '%', msg;
    END IF;

    SELECT COUNT(*) INTO conflict FROM (
        SELECT originalvalue FROM mpsn WHERE domain = in_domain LIMIT 1
    ) t;
    IF conflict > 0 THEN
        msg := 'Domain "' || in_domain || '" already exists in mpsn-table.';
        RAISE EXCEPTION '%', msg;
    END IF;

    INSERT INTO mpsn (originalvalue, pseudonym, domain, encoded_expiration_date)
        SELECT originalvalue, pseudonym, domain, encoded_expiration_date FROM psn WHERE domain = in_domain
        ON CONFLICT (originalvalue, pseudonym, domain) DO NOTHING;

    DELETE FROM psn WHERE domain = in_domain;

    UPDATE domain
    SET properties = COALESCE(properties, '')
        || (CASE WHEN COALESCE(properties, '') = '' OR RIGHT(properties, 1) = ';' THEN '' ELSE ';' END)
        || 'MULTI_PSN_DOMAIN=true;'
    WHERE name = in_domain;
END;
$$;

CREATE OR REPLACE PROCEDURE convert_to_single_psn_domain(IN in_domain VARCHAR(255))
LANGUAGE plpgsql
AS $$
DECLARE
    conflict INT;
    msg VARCHAR(255);
BEGIN
    SELECT CASE WHEN COUNT(*) > 0 THEN 0 ELSE 1 END INTO conflict FROM domain WHERE name = in_domain;
    IF conflict = 1 THEN
        msg := 'Domain "' || in_domain || '" not exists.';
        RAISE EXCEPTION '%', msg;
    END IF;

    SELECT COUNT(*) INTO conflict FROM (
        SELECT originalvalue FROM psn WHERE domain = in_domain LIMIT 1
    ) t;
    IF conflict > 0 THEN
        msg := 'Domain "' || in_domain || '" already exists in psn-table.';
        RAISE EXCEPTION '%', msg;
    END IF;

    SELECT COUNT(*) INTO conflict FROM (
        SELECT originalvalue FROM mpsn WHERE domain = in_domain
        GROUP BY originalvalue, domain HAVING COUNT(*) > 1 LIMIT 1
    ) t;
    IF conflict > 0 THEN
        msg := 'At least one originalValue is not unique in domain "' || in_domain || '".';
        RAISE EXCEPTION '%', msg;
    END IF;

    INSERT INTO psn (originalvalue, pseudonym, domain, encoded_expiration_date)
        SELECT originalvalue, pseudonym, domain, encoded_expiration_date FROM mpsn WHERE domain = in_domain
        ON CONFLICT (originalvalue, domain) DO NOTHING;

    DELETE FROM mpsn WHERE domain = in_domain;

    UPDATE domain
    SET properties = REGEXP_REPLACE(properties, 'MULTI_PSN_DOMAIN=[^;]+', 'MULTI_PSN_DOMAIN=false')
    WHERE name = in_domain;
END;
$$;
