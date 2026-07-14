-- =====================================================================
-- Seed data for the SQL-source de-identification TEST database.
--
-- This database is INTENTIONALLY full of fake PII so you can exercise the
-- SQL source feature end-to-end:
--   * direct identifiers   (names, SSN, MRN, email, phone)
--   * quasi-identifiers    (DOB, ZIP, sex)
--   * free-text PHI        (clinical notes  for NLP scrubbing)
--   * cross-table keys     (mrn / patient_mrn) so you can verify that gPAS
--                           pseudonymization stays CONSISTENT across tables
--                           and foreign-key joins survive de-identification.
--
-- All values are synthetic. Do not put real patient data here.
-- Runs once, on first container start (docker-entrypoint-initdb.d).
-- =====================================================================

CREATE SCHEMA IF NOT EXISTS clinic;
SET search_path TO clinic, public;

-- ---------------------------------------------------------------------
-- patients  one row per person; mrn is the cross-table linkage key.
-- ---------------------------------------------------------------------
CREATE TABLE patients (
    patient_id   SERIAL PRIMARY KEY,
    mrn          TEXT UNIQUE NOT NULL,
    full_name    TEXT NOT NULL,
    ssn          TEXT,
    date_of_birth DATE,
    sex          TEXT,
    email        TEXT,
    phone        TEXT,
    street       TEXT,
    city         TEXT,
    state        TEXT,
    zip          TEXT
);

INSERT INTO patients
    (mrn, full_name, ssn, date_of_birth, sex, email, phone, street, city, state, zip)
VALUES
    ('MRN-1001', 'John A. Doe',      '123-45-6789', '1980-02-04', 'M', 'john.doe@example.com',   '617-555-0142', '12 Maple St',     'Boston',     'MA', '02101'),
    ('MRN-1002', 'Jane B. Roe',      '987-65-4321', '1975-11-30', 'F', 'jane.roe@example.com',   '212-555-0199', '88 Oak Avenue',   'New York',   'NY', '10001'),
    ('MRN-1003', 'Carlos M. Vega',   '222-33-4444', '1990-07-19', 'M', 'carlos.vega@example.com','305-555-0177', '5 Palm Ct',       'Miami',      'FL', '33101'),
    ('MRN-1004', 'Aisha K. Rahman',  '555-66-7777', '1962-03-25', 'F', 'aisha.r@example.com',    '415-555-0123', '901 Pine Rd',     'San Francisco','CA','94105'),
    ('MRN-1005', 'Liang Wei',        '444-55-6666', '2001-12-12', 'M', 'liang.wei@example.com',  '773-555-0188', '34 Birch Ln',     'Chicago',    'IL', '60601'),
    ('MRN-1006', 'Maria Gonzalez',   '111-22-3333', '1988-09-08', 'F', 'maria.g@example.com',    '602-555-0150', '77 Cactus Way',   'Phoenix',    'AZ', '85001');

-- ---------------------------------------------------------------------
-- encounters  many per patient; references patients via patient_mrn.
-- The note column carries free-text PHI to test NLP scrubbing.
-- ---------------------------------------------------------------------
CREATE TABLE encounters (
    encounter_id SERIAL PRIMARY KEY,
    patient_mrn  TEXT NOT NULL REFERENCES patients(mrn),
    visit_date   DATE,
    department   TEXT,
    provider     TEXT,
    note         TEXT
);

INSERT INTO encounters (patient_mrn, visit_date, department, provider, note) VALUES
    ('MRN-1001', '2024-03-15', 'Cardiology',  'Dr. Susan Patel', 'John Doe (DOB 1980-02-04) reports chest pain. Contact 617-555-0142. Follow up in 2 weeks.'),
    ('MRN-1001', '2024-04-02', 'Cardiology',  'Dr. Susan Patel', 'Stress test normal. Patient John lives at 12 Maple St, Boston. SSN on file 123-45-6789.'),
    ('MRN-1002', '2024-02-20', 'Oncology',    'Dr. Mark Lee',    'Jane Roe seen for routine screening. Email jane.roe@example.com. No acute findings.'),
    ('MRN-1003', '2024-05-11', 'Orthopedics', 'Dr. Mark Lee',    'Carlos Vega, 33yo male, knee injury after soccer. Phone 305-555-0177.'),
    ('MRN-1004', '2024-01-09', 'Neurology',   'Dr. Anna Kim',    'Aisha Rahman presents with migraines. Prescribed sumatriptan. Reachable at 415-555-0123.'),
    ('MRN-1004', '2024-06-01', 'Neurology',   'Dr. Anna Kim',    'Follow-up: migraine frequency reduced. Patient Aisha doing well.'),
    ('MRN-1005', '2024-03-30', 'Dermatology', 'Dr. Susan Patel', 'Liang Wei, mole check. Benign. No further action.'),
    ('MRN-1006', '2024-04-18', 'Pediatrics',  'Dr. Anna Kim',    'Maria Gonzalez accompanied minor. Updated contact maria.g@example.com.');

-- ---------------------------------------------------------------------
-- lab_results  third table sharing the mrn key, for multi-table export.
-- ---------------------------------------------------------------------
CREATE TABLE lab_results (
    result_id    SERIAL PRIMARY KEY,
    patient_mrn  TEXT NOT NULL REFERENCES patients(mrn),
    collected_at DATE,
    test_name    TEXT,
    value        TEXT,
    units        TEXT
);

INSERT INTO lab_results (patient_mrn, collected_at, test_name, value, units) VALUES
    ('MRN-1001', '2024-03-15', 'Troponin I',     '0.02', 'ng/mL'),
    ('MRN-1001', '2024-04-02', 'LDL Cholesterol','118',  'mg/dL'),
    ('MRN-1002', '2024-02-20', 'CA-125',         '12',   'U/mL'),
    ('MRN-1003', '2024-05-11', 'CRP',            '3.1',  'mg/L'),
    ('MRN-1004', '2024-01-09', 'Hemoglobin',     '13.4', 'g/dL'),
    ('MRN-1005', '2024-03-30', 'Vitamin D',      '28',   'ng/mL'),
    ('MRN-1006', '2024-04-18', 'Glucose',        '92',   'mg/dL');

-- A table WITHOUT a single-column primary key, to exercise the OFFSET/LIMIT
-- read fallback path in the reflection layer.
CREATE TABLE visit_tags (
    patient_mrn TEXT NOT NULL,
    tag         TEXT NOT NULL
);
INSERT INTO visit_tags (patient_mrn, tag) VALUES
    ('MRN-1001', 'high-risk'), ('MRN-1001', 'cardiac'),
    ('MRN-1002', 'screening'), ('MRN-1004', 'chronic');
