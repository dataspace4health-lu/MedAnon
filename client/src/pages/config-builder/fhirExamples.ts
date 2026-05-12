// Bundled canonical FHIR R4 example resources — synthetic data only, no real PHI.

export interface FhirExample {
  resourceType: string;
  label: string;
  resource: Record<string, unknown>;
}

export const FHIR_EXAMPLES: FhirExample[] = [
  {
    resourceType: 'Patient',
    label: 'Patient',
    resource: {
      resourceType: 'Patient',
      id: 'example-patient-1',
      identifier: [{ use: 'official', system: 'http://hospital.example/mrn', value: 'MRN-12345' }],
      active: true,
      name: [{ use: 'official', family: 'Chalmers', given: ['Peter', 'James'] }],
      telecom: [
        { system: 'phone', value: '+1-555-123-4567', use: 'home' },
        { system: 'email', value: 'p.chalmers@example.com' },
      ],
      gender: 'male',
      birthDate: '1974-12-25',
      address: [
        {
          use: 'home',
          line: ['534 Erewhon St'],
          city: 'PleasantVille',
          state: 'MA',
          postalCode: '02906',
          country: 'US',
        },
      ],
      maritalStatus: {
        coding: [{ system: 'http://terminology.hl7.org/CodeSystem/v3-MaritalStatus', code: 'M', display: 'Married' }],
      },
      contact: [
        {
          name: { family: 'du Marché', given: ['Bénédicte'] },
          telecom: [{ system: 'phone', value: '+33-555-000-111' }],
        },
      ],
      communication: [{ language: { coding: [{ code: 'fr', display: 'French' }] } }],
      generalPractitioner: [{ reference: 'Practitioner/pract-1', display: 'Dr. John Smith' }],
    },
  },
  {
    resourceType: 'Observation',
    label: 'Observation',
    resource: {
      resourceType: 'Observation',
      id: 'obs-blood-pressure',
      status: 'final',
      category: [
        {
          coding: [
            {
              system: 'http://terminology.hl7.org/CodeSystem/observation-category',
              code: 'vital-signs',
              display: 'Vital Signs',
            },
          ],
        },
      ],
      code: { coding: [{ system: 'http://loinc.org', code: '85354-9', display: 'Blood pressure panel' }] },
      subject: { reference: 'Patient/example-patient-1', display: 'Peter Chalmers' },
      effectiveDateTime: '2024-03-15T09:30:00Z',
      issued: '2024-03-15T10:00:00Z',
      performer: [{ reference: 'Practitioner/pract-1', display: 'Dr. John Smith' }],
      component: [
        {
          code: { coding: [{ code: '8480-6', display: 'Systolic BP' }] },
          valueQuantity: { value: 120, unit: 'mmHg', system: 'http://unitsofmeasure.org', code: 'mm[Hg]' },
        },
      ],
      note: [{ text: 'Patient was slightly anxious. Values within normal range.' }],
    },
  },
  {
    resourceType: 'Condition',
    label: 'Condition',
    resource: {
      resourceType: 'Condition',
      id: 'condition-diabetes',
      clinicalStatus: {
        coding: [{ system: 'http://terminology.hl7.org/CodeSystem/condition-clinical', code: 'active', display: 'Active' }],
      },
      verificationStatus: {
        coding: [{ system: 'http://terminology.hl7.org/CodeSystem/condition-ver-status', code: 'confirmed' }],
      },
      code: {
        coding: [{ system: 'http://snomed.info/sct', code: '44054006', display: 'Diabetes mellitus type 2' }],
        text: 'Type 2 Diabetes',
      },
      subject: { reference: 'Patient/example-patient-1', display: 'Peter Chalmers' },
      onsetDateTime: '2015-06-01',
      recordedDate: '2015-06-15',
      recorder: { reference: 'Practitioner/pract-1' },
      note: [{ text: 'Well-controlled with metformin. Patient counseled on diet.' }],
    },
  },
  {
    resourceType: 'Encounter',
    label: 'Encounter',
    resource: {
      resourceType: 'Encounter',
      id: 'encounter-outpatient-1',
      status: 'finished',
      class: {
        system: 'http://terminology.hl7.org/CodeSystem/v3-ActCode',
        code: 'AMB',
        display: 'ambulatory',
      },
      subject: { reference: 'Patient/example-patient-1', display: 'Peter Chalmers' },
      participant: [
        {
          type: [{ coding: [{ code: 'ATND' }] }],
          individual: { reference: 'Practitioner/pract-1', display: 'Dr. John Smith' },
        },
      ],
      period: { start: '2024-03-15T09:00:00Z', end: '2024-03-15T10:30:00Z' },
      reasonCode: [
        { coding: [{ system: 'http://snomed.info/sct', code: '44054006', display: 'Diabetes mellitus type 2' }] },
      ],
      location: [{ location: { reference: 'Location/loc-1', display: 'City Hospital - Outpatient' } }],
    },
  },
  {
    resourceType: 'MedicationRequest',
    label: 'MedicationRequest',
    resource: {
      resourceType: 'MedicationRequest',
      id: 'medreq-metformin',
      status: 'active',
      intent: 'order',
      medicationCodeableConcept: {
        coding: [{ system: 'http://www.nlm.nih.gov/research/umls/rxnorm', code: '860975', display: 'Metformin 500 MG' }],
      },
      subject: { reference: 'Patient/example-patient-1', display: 'Peter Chalmers' },
      authoredOn: '2024-03-15',
      requester: { reference: 'Practitioner/pract-1', display: 'Dr. John Smith' },
      dosageInstruction: [
        {
          text: 'Take 1 tablet twice daily with meals',
          timing: { repeat: { frequency: 2, period: 1, periodUnit: 'd' } },
        },
      ],
      note: [{ text: 'Patient tolerating medication well.' }],
    },
  },
  {
    resourceType: 'AllergyIntolerance',
    label: 'AllergyIntolerance',
    resource: {
      resourceType: 'AllergyIntolerance',
      id: 'allergy-penicillin',
      clinicalStatus: { coding: [{ code: 'active' }] },
      type: 'allergy',
      category: ['medication'],
      criticality: 'high',
      code: {
        coding: [{ system: 'http://www.nlm.nih.gov/research/umls/rxnorm', code: '7980', display: 'Penicillin' }],
      },
      patient: { reference: 'Patient/example-patient-1', display: 'Peter Chalmers' },
      recordedDate: '2010-03-01',
      recorder: { reference: 'Practitioner/pract-1' },
      note: [{ text: 'Reported anaphylaxis as a child.' }],
    },
  },
  {
    resourceType: 'Procedure',
    label: 'Procedure',
    resource: {
      resourceType: 'Procedure',
      id: 'proc-appendectomy',
      status: 'completed',
      code: { coding: [{ system: 'http://snomed.info/sct', code: '80146002', display: 'Appendectomy' }] },
      subject: { reference: 'Patient/example-patient-1', display: 'Peter Chalmers' },
      performedDateTime: '2018-08-10T14:00:00Z',
      recorder: { reference: 'Practitioner/pract-1', display: 'Dr. John Smith' },
      performer: [{ actor: { reference: 'Practitioner/pract-1', display: 'Dr. John Smith' } }],
      location: { reference: 'Location/loc-1', display: 'City Hospital' },
      note: [{ text: 'Laparoscopic appendectomy performed without complications.' }],
    },
  },
  {
    resourceType: 'DiagnosticReport',
    label: 'DiagnosticReport',
    resource: {
      resourceType: 'DiagnosticReport',
      id: 'diag-hba1c',
      status: 'final',
      category: [{ coding: [{ system: 'http://loinc.org', code: 'LAB', display: 'Laboratory' }] }],
      code: { coding: [{ system: 'http://loinc.org', code: '4548-4', display: 'Hemoglobin A1c/Hemoglobin.total in Blood' }] },
      subject: { reference: 'Patient/example-patient-1', display: 'Peter Chalmers' },
      effectiveDateTime: '2024-03-15',
      issued: '2024-03-15T11:45:00Z',
      performer: [{ reference: 'Practitioner/pract-1', display: 'Dr. John Smith' }],
      conclusion: 'HbA1c within target range for well-controlled type 2 diabetes.',
    },
  },
  {
    resourceType: 'Practitioner',
    label: 'Practitioner',
    resource: {
      resourceType: 'Practitioner',
      id: 'pract-1',
      identifier: [{ system: 'http://hl7.org/fhir/sid/us-npi', value: '1234567890' }],
      active: true,
      name: [{ use: 'official', family: 'Smith', given: ['John'], prefix: ['Dr.'] }],
      telecom: [{ system: 'phone', value: '+1-555-987-6543', use: 'work' }],
      address: [
        { use: 'work', line: ['456 Medical Center Blvd'], city: 'Boston', state: 'MA', postalCode: '02101' },
      ],
      gender: 'male',
      birthDate: '1965-04-22',
      qualification: [
        {
          code: {
            coding: [
              { system: 'http://terminology.hl7.org/CodeSystem/v2-0360', code: 'MD', display: 'Doctor of Medicine' },
            ],
          },
        },
      ],
    },
  },
];
