-- =====================================================================
-- LARGE multi-table PII seed for the SQL-source de-identification test DB.
--
-- Runs AFTER 01_seed.sql (alphabetical order in docker-entrypoint-initdb.d).
-- Adds bulk volume + many more PII-bearing tables so you can exercise the
-- SQL-source feature against a realistic, identifier-heavy schema:
--
--   patients          +500 generated rows (total 506)   direct + quasi IDs
--   encounters        ~3 per patient                      free-text PHI notes
--   lab_results       ~2 per patient                      clinical values
--   insurance         1 per patient                       member id, group, SSN
--   appointments      ~2 per patient                      contact + scheduling
--   prescriptions     ~2 per patient                      drug + prescriber DEA
--   next_of_kin       1 per patient                       3rd-party PII
--   billing           1 per patient                       card last4, IBAN-ish
--   providers         50 staff                            NPI, DEA, email, phone
--   audit_access_log  ~4 per patient                      IP, user agent, actor
--   lab_notes         free-text only, no single-col PK    OFFSET/LIMIT fallback
--
-- All values are SYNTHETIC. Cross-table key is mrn / patient_mrn so you can
-- verify gPAS pseudonymization stays consistent and FK joins survive de-id.
-- =====================================================================

SET search_path TO clinic, public;

-- Deterministic-ish helpers driven by a row index N (1..500). Kept inline so
-- the file is a single self-contained script with no extra functions to clean.

-- ---------------------------------------------------------------------
-- Bulk patients: MRN-2001 .. MRN-2500
-- ---------------------------------------------------------------------
INSERT INTO patients
    (mrn, full_name, ssn, date_of_birth, sex, email, phone, street, city, state, zip)
SELECT
    'MRN-' || (2000 + n),
    (ARRAY['James','Mary','Robert','Patricia','Michael','Jennifer','William','Linda',
           'David','Elizabeth','Wei','Aisha','Carlos','Fatima','Yuki','Omar',
           'Sofia','Ravi','Chloe','Hassan'])[1 + (n % 20)]
        || ' ' ||
    (ARRAY['Smith','Johnson','Williams','Brown','Jones','Garcia','Miller','Davis',
           'Rodriguez','Martinez','Nguyen','Patel','Kim','Khan','Okafor','Rossi',
           'Andersson','Cohen','Silva','Tanaka'])[1 + ((n / 3) % 20)],
    -- SSN AAA-GG-SSSS, zero-padded synthetic
    lpad(((100 + (n * 7) % 800))::text, 3, '0') || '-' ||
    lpad(((n * 13) % 100)::text, 2, '0') || '-' ||
    lpad(((n * 9173) % 10000)::text, 4, '0'),
    DATE '1940-01-01' + ((n * 137) % 26000),          -- DOB spread ~71 yrs
    (ARRAY['M','F','O'])[1 + (n % 3)],
    'patient' || (2000 + n) || '@example.com',
    -- US-style phone
    '(' || lpad(((200 + (n * 3) % 700))::text, 3, '0') || ') ' ||
    lpad(((n * 17) % 1000)::text, 3, '0') || '-' ||
    lpad(((n * 53) % 10000)::text, 4, '0'),
    (n % 9000 + 1) || ' ' ||
        (ARRAY['Maple','Oak','Pine','Cedar','Elm','Birch','Walnut','Cherry',
               'Spruce','Aspen'])[1 + (n % 10)] || ' ' ||
        (ARRAY['St','Ave','Rd','Ln','Ct','Blvd','Way','Dr'])[1 + (n % 8)],
    (ARRAY['Boston','New York','Miami','San Francisco','Chicago','Phoenix',
           'Seattle','Austin','Denver','Atlanta'])[1 + (n % 10)],
    (ARRAY['MA','NY','FL','CA','IL','AZ','WA','TX','CO','GA'])[1 + (n % 10)],
    lpad(((1000 + (n * 31) % 89000))::text, 5, '0')
FROM generate_series(1, 500) AS n;

-- ---------------------------------------------------------------------
-- providers — clinical staff with their own PII (NPI, DEA, email, phone)
-- ---------------------------------------------------------------------
CREATE TABLE providers (
    provider_id  SERIAL PRIMARY KEY,
    full_name    TEXT NOT NULL,
    npi          TEXT,            -- 10-digit National Provider Identifier
    dea          TEXT,            -- DEA registration number
    specialty    TEXT,
    email        TEXT,
    phone        TEXT
);

INSERT INTO providers (full_name, npi, dea, specialty, email, phone)
SELECT
    'Dr. ' ||
    (ARRAY['Susan','Mark','Anna','Raj','Lena','Tom','Grace','Hugo','Nadia','Eli'])[1 + (n % 10)]
        || ' ' ||
    (ARRAY['Patel','Lee','Kim','Mehta','Olsen','Ford','Reyes','Bauer','Haddad','Stone'])[1 + ((n / 2) % 10)],
    lpad(((1000000000 + n * 7919) % 10000000000)::text, 10, '0'),
    (ARRAY['A','B','F','M'])[1 + (n % 4)]
        || (ARRAY['P','L','K','R','S'])[1 + (n % 5)]
        || lpad(((n * 311) % 10000000)::text, 7, '0'),
    (ARRAY['Cardiology','Oncology','Neurology','Orthopedics','Dermatology',
           'Pediatrics','Radiology','Psychiatry','Endocrinology','Urology'])[1 + (n % 10)],
    'provider' || n || '@hospital.example.org',
    '(' || lpad(((300 + n % 600))::text, 3, '0') || ') 555-' ||
        lpad(((n * 41) % 10000)::text, 4, '0')
FROM generate_series(1, 50) AS n;

-- ---------------------------------------------------------------------
-- Bulk encounters (~3 per generated patient) with free-text PHI notes
-- ---------------------------------------------------------------------
INSERT INTO encounters (patient_mrn, visit_date, department, provider, note)
SELECT
    p.mrn,
    DATE '2023-01-01' + ((p.patient_id * 37 + v * 53) % 800),
    (ARRAY['Cardiology','Oncology','Neurology','Orthopedics','Dermatology',
           'Pediatrics','Emergency','Primary Care'])[1 + ((p.patient_id + v) % 8)],
    'Dr. ' ||
        (ARRAY['Patel','Lee','Kim','Mehta','Olsen','Ford','Reyes'])[1 + ((p.patient_id + v) % 7)],
    -- Free-text note packed with PII for NLP scrubbing.
    p.full_name || ' (DOB ' || to_char(p.date_of_birth, 'YYYY-MM-DD')
        || ') seen on visit ' || v || '. Reachable at ' || p.phone
        || ' or ' || p.email || '. Lives at ' || p.street || ', ' || p.city
        || ', ' || p.state || ' ' || p.zip || '. SSN ' || p.ssn
        || '. ' ||
        (ARRAY['Reports chest pain; ECG ordered.',
               'Routine screening, no acute findings.',
               'Follow-up in 2 weeks with Dr. Lee.',
               'Prescribed medication; counseled on side effects.',
               'Referred to specialist for further evaluation.'])[1 + ((p.patient_id + v) % 5)]
FROM patients p
CROSS JOIN generate_series(1, 3) AS v
WHERE p.mrn LIKE 'MRN-2%';

-- ---------------------------------------------------------------------
-- Bulk lab_results (~2 per generated patient)
-- ---------------------------------------------------------------------
INSERT INTO lab_results (patient_mrn, collected_at, test_name, value, units)
SELECT
    p.mrn,
    DATE '2023-06-01' + ((p.patient_id * 19 + v * 7) % 600),
    (ARRAY['Troponin I','LDL Cholesterol','HbA1c','CRP','Hemoglobin',
           'Vitamin D','Glucose','TSH','Creatinine','Potassium'])[1 + ((p.patient_id + v) % 10)],
    (round((random() * 200)::numeric, 1))::text,
    (ARRAY['ng/mL','mg/dL','%','mg/L','g/dL','U/mL','mmol/L'])[1 + ((p.patient_id + v) % 7)]
FROM patients p
CROSS JOIN generate_series(1, 2) AS v
WHERE p.mrn LIKE 'MRN-2%';

-- ---------------------------------------------------------------------
-- insurance — one policy per patient (member id, group, subscriber SSN)
-- ---------------------------------------------------------------------
CREATE TABLE insurance (
    policy_id     SERIAL PRIMARY KEY,
    patient_mrn   TEXT NOT NULL REFERENCES patients(mrn),
    payer         TEXT,
    member_id     TEXT,
    group_number  TEXT,
    subscriber_name TEXT,
    subscriber_ssn  TEXT,
    effective_date  DATE
);

INSERT INTO insurance
    (patient_mrn, payer, member_id, group_number, subscriber_name, subscriber_ssn, effective_date)
SELECT
    p.mrn,
    (ARRAY['Aetna','BlueCross','Cigna','UnitedHealth','Kaiser','Humana'])[1 + (p.patient_id % 6)],
    'MBR' || lpad(p.patient_id::text, 8, '0'),
    'GRP-' || lpad(((p.patient_id * 7) % 9999)::text, 4, '0'),
    p.full_name,
    p.ssn,
    DATE '2022-01-01' + (p.patient_id % 700)
FROM patients p;

-- ---------------------------------------------------------------------
-- appointments — scheduling + direct contact (~2 per patient)
-- ---------------------------------------------------------------------
CREATE TABLE appointments (
    appt_id      SERIAL PRIMARY KEY,
    patient_mrn  TEXT NOT NULL REFERENCES patients(mrn),
    scheduled_at TIMESTAMP,
    provider     TEXT,
    reason       TEXT,
    contact_phone TEXT,
    contact_email TEXT,
    status       TEXT
);

INSERT INTO appointments
    (patient_mrn, scheduled_at, provider, reason, contact_phone, contact_email, status)
SELECT
    p.mrn,
    TIMESTAMP '2024-01-01 08:00' + ((p.patient_id * 11 + v * 90) % 5000 || ' minutes')::interval,
    'Dr. ' || (ARRAY['Patel','Lee','Kim','Mehta','Olsen'])[1 + ((p.patient_id + v) % 5)],
    (ARRAY['Annual physical','Follow-up','Lab review','New symptom','Vaccination'])[1 + ((p.patient_id + v) % 5)],
    p.phone,
    p.email,
    (ARRAY['scheduled','completed','cancelled','no-show'])[1 + ((p.patient_id + v) % 4)]
FROM patients p
CROSS JOIN generate_series(1, 2) AS v;

-- ---------------------------------------------------------------------
-- prescriptions — drug + prescriber DEA (~2 per patient)
-- ---------------------------------------------------------------------
CREATE TABLE prescriptions (
    rx_id        SERIAL PRIMARY KEY,
    patient_mrn  TEXT NOT NULL REFERENCES patients(mrn),
    drug_name    TEXT,
    dose         TEXT,
    prescriber   TEXT,
    prescriber_dea TEXT,
    written_date DATE
);

INSERT INTO prescriptions
    (patient_mrn, drug_name, dose, prescriber, prescriber_dea, written_date)
SELECT
    p.mrn,
    (ARRAY['Lisinopril','Atorvastatin','Metformin','Sertraline','Sumatriptan',
           'Amoxicillin','Omeprazole','Albuterol','Gabapentin','Levothyroxine'])[1 + ((p.patient_id + v) % 10)],
    (ARRAY['10mg','20mg','40mg','500mg','100mg','5mg'])[1 + ((p.patient_id + v) % 6)],
    'Dr. ' || (ARRAY['Patel','Lee','Kim','Mehta','Olsen'])[1 + ((p.patient_id + v) % 5)],
    'B' || (ARRAY['P','L','K','M','O'])[1 + ((p.patient_id + v) % 5)]
        || lpad(((p.patient_id * 131 + v) % 10000000)::text, 7, '0'),
    DATE '2023-01-01' + ((p.patient_id * 23 + v * 41) % 700)
FROM patients p
CROSS JOIN generate_series(1, 2) AS v;

-- ---------------------------------------------------------------------
-- next_of_kin — third-party PII (name, relationship, phone)
-- ---------------------------------------------------------------------
CREATE TABLE next_of_kin (
    nok_id       SERIAL PRIMARY KEY,
    patient_mrn  TEXT NOT NULL REFERENCES patients(mrn),
    name         TEXT,
    relationship TEXT,
    phone        TEXT,
    email        TEXT
);

INSERT INTO next_of_kin (patient_mrn, name, relationship, phone, email)
SELECT
    p.mrn,
    (ARRAY['Pat','Sam','Alex','Jordan','Casey','Morgan','Taylor','Jamie'])[1 + (p.patient_id % 8)]
        || ' ' || split_part(p.full_name, ' ', 2),
    (ARRAY['Spouse','Parent','Child','Sibling','Friend'])[1 + (p.patient_id % 5)],
    '(' || lpad(((200 + p.patient_id % 700))::text, 3, '0') || ') 555-' ||
        lpad(((p.patient_id * 29) % 10000)::text, 4, '0'),
    'kin' || p.patient_id || '@example.com'
FROM patients p;

-- ---------------------------------------------------------------------
-- billing — financial identifiers (card last4, synthetic IBAN-ish)
-- ---------------------------------------------------------------------
CREATE TABLE billing (
    invoice_id   SERIAL PRIMARY KEY,
    patient_mrn  TEXT NOT NULL REFERENCES patients(mrn),
    amount_usd   NUMERIC(10,2),
    card_last4   TEXT,
    iban         TEXT,
    billed_at    DATE,
    paid         BOOLEAN
);

INSERT INTO billing (patient_mrn, amount_usd, card_last4, iban, billed_at, paid)
SELECT
    p.mrn,
    round((50 + random() * 9950)::numeric, 2),
    lpad(((p.patient_id * 7919) % 10000)::text, 4, '0'),
    'GB' || lpad((p.patient_id % 100)::text, 2, '0') || 'BANK' ||
        lpad(((p.patient_id * 12345) % 100000000)::text, 8, '0'),
    DATE '2024-01-01' + (p.patient_id % 365),
    (p.patient_id % 3 = 0)
FROM patients p;

-- ---------------------------------------------------------------------
-- audit_access_log — IPs, user agents, actor identities (~4 per patient)
-- ---------------------------------------------------------------------
CREATE TABLE audit_access_log (
    log_id       SERIAL PRIMARY KEY,
    patient_mrn  TEXT NOT NULL REFERENCES patients(mrn),
    accessed_at  TIMESTAMP,
    actor_user   TEXT,
    actor_email  TEXT,
    client_ip    TEXT,
    user_agent   TEXT,
    action       TEXT
);

INSERT INTO audit_access_log
    (patient_mrn, accessed_at, actor_user, actor_email, client_ip, user_agent, action)
SELECT
    p.mrn,
    TIMESTAMP '2024-01-01 00:00' + ((p.patient_id * 13 + v * 360) % 500000 || ' minutes')::interval,
    'user' || ((p.patient_id + v) % 80),
    'staff' || ((p.patient_id + v) % 80) || '@hospital.example.org',
    -- Synthetic public-ish IPv4
    (1 + (p.patient_id % 223)) || '.' || ((p.patient_id * 3) % 256) || '.' ||
        ((p.patient_id * 7 + v) % 256) || '.' || ((p.patient_id * 11 + v) % 256),
    (ARRAY['Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
           'Mozilla/5.0 (Macintosh; Intel Mac OS X 13_0)',
           'Mozilla/5.0 (X11; Linux x86_64)',
           'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)'])[1 + ((p.patient_id + v) % 4)],
    (ARRAY['view','edit','export','print'])[1 + ((p.patient_id + v) % 4)]
FROM patients p
CROSS JOIN generate_series(1, 4) AS v;

-- ---------------------------------------------------------------------
-- lab_notes — free-text PHI ONLY, no single-column PK (OFFSET/LIMIT path)
-- ---------------------------------------------------------------------
CREATE TABLE lab_notes (
    patient_mrn  TEXT NOT NULL,
    note_date    DATE,
    note         TEXT
);

INSERT INTO lab_notes (patient_mrn, note_date, note)
SELECT
    p.mrn,
    DATE '2024-02-01' + (p.patient_id % 300),
    'Specimen for ' || p.full_name || ' (MRN ' || p.mrn || ', DOB '
        || to_char(p.date_of_birth, 'YYYY-MM-DD') || '). Contact ' || p.phone
        || '. Critical value called to ' || p.email || '.'
FROM patients p
WHERE p.patient_id % 2 = 0;

-- ---------------------------------------------------------------------
-- Row-count summary (visible in container init logs)
-- ---------------------------------------------------------------------
DO $$
DECLARE r RECORD;
BEGIN
    RAISE NOTICE '--- clinic seed row counts ---';
    FOR r IN
        SELECT relname, n_live_tup
        FROM pg_stat_user_tables
        WHERE schemaname = 'clinic'
        ORDER BY relname
    LOOP
        RAISE NOTICE '  %: ~% rows', r.relname, r.n_live_tup;
    END LOOP;
END $$;
