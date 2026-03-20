import unittest
import re
import os
from unittest.mock import patch
from pipeline.config import Settings
from pipeline.processor import process_data
from utils.fhirpath import read_resource_from_file
import dateutil.parser as parser
from datetime import timedelta
from rich import print


class TestConfig(unittest.TestCase):
    """
    Testing settings applied to FHIR resources
    """

    def test_read_settings(self):
        print(f"=========== TEST READ SETTINGS ===========")
        settings = Settings()
        print(f"Checking settings reading...\t", end="", flush=True)
        self.assertIsNotNone(settings.rules)
        print(f":thumbs_up:")

    def test_read_lists(self):
        print(f"=========== TEST READ LIST ===========")
        config_filename = 'test/config/redact.yaml'
        resource_filename = '../../../data/sample_fhir_data/patient_list.json'
        resource = read_resource_from_file(resource_filename)
        settings = Settings(config_filename)
        ret = process_data(resource, settings)
        print(f"Checking FHIR resources list reading...\t", end="", flush=True)
        self.assertRaises(KeyError, lambda: ret[0]['name'])
        print(f":thumbs_up:")

    def test_deidentify_redact(self):
        print(f"========== TEST REDACT ==========")
        config_filename = 'test/config/redact.yaml'
        resource_filename = '../../../data/sample_fhir_data/simple_patient.json'
        resource = read_resource_from_file(resource_filename)
        settings = Settings(config_filename)
        ret = process_data(resource, settings)
        print(f"Checking redact...\t", end="", flush=True)
        self.assertRaises(KeyError, lambda: ret['name'])
        print(f":thumbs_up:")

    def test_deidentify_perturb(self):
        print(f"========== TEST PERTURB =========")
        config_filename = 'test/config/perturb.yaml'
        resource_filename = '../../../data/sample_fhir_data/patient_R5DB.json'
        resource = read_resource_from_file(resource_filename)
        settings = Settings(config_filename)
        ret = process_data(resource, settings)
        perturbed_date = parser.parse(ret['birthDate'])
        ref_date = parser.parse('1974-12-25')
        min_date = ref_date - timedelta(days=5)
        max_date = ref_date + timedelta(days=10)
        print(f"Checking perturb...\t", end="", flush=True)
        self.assertTrue(perturbed_date <= max_date)
        self.assertTrue(perturbed_date >= min_date)
        print(f":thumbs_up:")

    def test_deidentify_cryptohash(self):
        print(f"======== TEST CRYPTOHASH ========")
        config_filename = 'test/config/cryptohash.yaml'
        resource_filename = '../../../data/sample_fhir_data/simple_patient.json'
        resource = read_resource_from_file(resource_filename)
        settings = Settings(config_filename)
        # Unset MEDANON_HASH_KEY so the test always uses plain SHA3-256,
        # regardless of the environment the tests are run in.
        with patch.dict(os.environ, {'MEDANON_HASH_KEY': ''}, clear=False):
            ret = process_data(resource, settings)
        print(f"Checking cryptohash...\t", end="", flush=True)
        self.assertEqual(
            ret['name'][0], 'b7340931fb4ee512d5d5f68f6da7a027c5ba8dd8a8d5ea4705416f0b85e1b9ca')
        self.assertEqual(
            ret['name'][1], '6f4bae1f49ee29890cbfcf8ffb26eccc2520cb543fa30a28458e2952f40b7ea3')
        self.assertEqual(
            ret['name'][2], 'dad3235c168df9f3ad295af27f18866e09c4c41009ca555c7706d90474c02626')
        print(f":thumbs_up:")

    def test_deidentify_substitute(self):
        print(f"======== TEST SUBSTITUTE ========")
        config_filename = 'test/config/substitute.yaml'
        resource_filename = '../../../data/sample_fhir_data/simple_patient.json'
        resource = read_resource_from_file(resource_filename)
        settings = Settings(config_filename)
        ret = process_data(resource, settings)
        print(f"Checking substitute...\t", end="", flush=True)
        self.assertEqual(ret['name'][0]['family'], 'foo')
        self.assertEqual(ret['name'][2]['family'], 'foo')
        print(f":thumbs_up:")

    def test_generalize_date_year(self):
        print(f"======== TEST GENERALIZE DATE_YEAR ========")
        settings = type('S', (), {
            'rules': [{
                'match': 'Patient.birthDate',
                'action': 'generalize',
                'params': {'strategy': 'date_year'},
            }]
        })()
        resource = {'resourceType': 'Patient', 'id': 'p1', 'birthDate': '1991-01-04'}
        ret = process_data(resource, settings)
        print(f"Checking generalize date_year...\t", end="", flush=True)
        self.assertEqual(ret['birthDate'], '1991')
        print(f":thumbs_up:")

    def test_generalize_date_year_month(self):
        print(f"======== TEST GENERALIZE DATE_YEAR_MONTH ========")
        settings = type('S', (), {
            'rules': [{
                'match': 'Patient.birthDate',
                'action': 'generalize',
                'params': {'strategy': 'date_year_month'},
            }]
        })()
        resource = {'resourceType': 'Patient', 'id': 'p1', 'birthDate': '1980-02-04'}
        ret = process_data(resource, settings)
        print(f"Checking generalize date_year_month...\t", end="", flush=True)
        self.assertEqual(ret['birthDate'], '1980-02')
        print(f":thumbs_up:")

    def test_generalize_datetime_to_year(self):
        print(f"======== TEST GENERALIZE DATETIME TO YEAR ========")
        settings = type('S', (), {
            'rules': [{
                'match': 'Patient.extension.valueDateTime',
                'action': 'generalize',
                'params': {'strategy': 'date_year'},
            }]
        })()
        resource = {
            'resourceType': 'Patient', 'id': 'p1',
            'extension': [{
                'url': 'http://hl7.org/fhir/StructureDefinition/patient-birthTime',
                'valueDateTime': '1991-01-04T00:00:00'
            }]
        }
        ret = process_data(resource, settings)
        print(f"Checking generalize dateTime to year...\t", end="", flush=True)
        self.assertEqual(ret['extension'][0]['valueDateTime'], '1991')
        print(f":thumbs_up:")

    def test_generalize_age_bracket(self):
        print(f"======== TEST GENERALIZE AGE_BRACKET ========")
        settings = type('S', (), {
            'rules': [{
                'match': 'Patient.birthDate',
                'action': 'generalize',
                'params': {'strategy': 'age_bracket', 'bracket_size': 10},
            }]
        })()
        resource = {'resourceType': 'Patient', 'id': 'p1', 'birthDate': '1991-01-04'}
        ret = process_data(resource, settings)
        print(f"Checking generalize age_bracket...\t", end="", flush=True)
        # Person born 1991 is 34-35 in 2026, bracket 30-39
        self.assertRegex(ret['birthDate'], r'^\d+-\d+$')
        print(f":thumbs_up:")

    def test_generalize_number_round(self):
        print(f"======== TEST GENERALIZE NUMBER_ROUND ========")
        settings = type('S', (), {
            'rules': [{
                'match': 'Observation.valueQuantity.value',
                'action': 'generalize',
                'params': {'strategy': 'number_round', 'precision': 10},
            }]
        })()
        resource = {'resourceType': 'Observation', 'id': 'o1',
                    'valueQuantity': {'value': 94, 'unit': 'cm'}}
        ret = process_data(resource, settings)
        print(f"Checking generalize number_round...\t", end="", flush=True)
        self.assertEqual(ret['valueQuantity']['value'], 90)
        print(f":thumbs_up:")

    def test_generalize_zip_prefix(self):
        print(f"======== TEST GENERALIZE ZIP_PREFIX ========")
        settings = type('S', (), {
            'rules': [{
                'match': 'Patient.address.postalCode',
                'action': 'generalize',
                'params': {'strategy': 'zip_prefix', 'prefix_len': 3},
            }]
        })()
        resource = {'resourceType': 'Patient', 'id': 'p1',
                    'address': [{'postalCode': '12345', 'city': 'Berlin'}]}
        ret = process_data(resource, settings)
        print(f"Checking generalize zip_prefix...\t", end="", flush=True)
        self.assertEqual(ret['address'][0]['postalCode'], '123')
        # city is untouched
        self.assertEqual(ret['address'][0]['city'], 'Berlin')
        print(f":thumbs_up:")



class TestScrubText(unittest.TestCase):
    """Tests for the scrub_text action."""

    def _settings(self, match, mode='text', patterns='all', extra=None):
        p = {'mode': mode, 'patterns': patterns}
        if extra:
            p.update(extra)
        return type('S', (), {'rules': [{'match': match, 'action': 'scrub_text', 'params': p}]})()

    def test_scrub_iso_date(self):
        print("======== TEST SCRUB_TEXT ISO DATE ========")
        s = self._settings('Observation.note.text', patterns='date_iso')
        resource = {'resourceType': 'Observation', 'id': 'o1',
                    'note': [{'text': 'Patient seen on 1991-01-04 for follow-up.'}]}
        ret = process_data(resource, s)
        print("Checking ISO date scrubbing...\t", end="", flush=True)
        self.assertNotIn('1991-01-04', ret['note'][0]['text'])
        self.assertRegex(ret['note'][0]['text'], r'\[\[DATE_\d+\]\]')
        print(":thumbs_up:")

    def test_scrub_us_date(self):
        print("======== TEST SCRUB_TEXT US DATE ========")
        s = self._settings('Condition.note.text', patterns='date_us')
        resource = {'resourceType': 'Condition', 'id': 'c1',
                    'note': [{'text': 'Follow-up on 01/04/1991.'}]}
        ret = process_data(resource, s)
        print("Checking US date scrubbing...\t", end="", flush=True)
        self.assertNotIn('01/04/1991', ret['note'][0]['text'])
        self.assertRegex(ret['note'][0]['text'], r'\[\[DATE_\d+\]\]')
        print(":thumbs_up:")

    def test_scrub_phone(self):
        print("======== TEST SCRUB_TEXT PHONE ========")
        s = self._settings('Observation.note.text', patterns='phone')
        resource = {'resourceType': 'Observation', 'id': 'o1',
                    'note': [{'text': 'Call Dr. Smith at (555) 867-5309 for results.'}]}
        ret = process_data(resource, s)
        print("Checking phone scrubbing...\t", end="", flush=True)
        self.assertNotIn('(555) 867-5309', ret['note'][0]['text'])
        self.assertRegex(ret['note'][0]['text'], r'\[\[PHONE_\d+\]\]')
        print(":thumbs_up:")

    def test_scrub_ssn(self):
        print("======== TEST SCRUB_TEXT SSN ========")
        s = self._settings('Observation.note.text', patterns='ssn')
        resource = {'resourceType': 'Observation', 'id': 'o1',
                    'note': [{'text': 'SSN recorded as 123-45-6789 in chart.'}]}
        ret = process_data(resource, s)
        print("Checking SSN scrubbing...\t", end="", flush=True)
        self.assertNotIn('123-45-6789', ret['note'][0]['text'])
        self.assertRegex(ret['note'][0]['text'], r'\[\[SSN_\d+\]\]')
        print(":thumbs_up:")

    def test_scrub_email(self):
        print("======== TEST SCRUB_TEXT EMAIL ========")
        s = self._settings('Observation.note.text', patterns='email')
        resource = {'resourceType': 'Observation', 'id': 'o1',
                    'note': [{'text': 'Contact patient at john.doe@example.com for consent.'}]}
        ret = process_data(resource, s)
        print("Checking email scrubbing...\t", end="", flush=True)
        self.assertNotIn('john.doe@example.com', ret['note'][0]['text'])
        self.assertRegex(ret['note'][0]['text'], r'\[\[EMAIL_\d+\]\]')
        print(":thumbs_up:")

    def test_scrub_custom_names(self):
        print("======== TEST SCRUB_TEXT CUSTOM NAMES ========")
        s = self._settings('Observation.note.text', patterns='all',
                           extra={'names': ['John', 'Smith']})
        resource = {'resourceType': 'Observation', 'id': 'o1',
                    'note': [{'text': 'Patient John Smith presents with cough.'}]}
        ret = process_data(resource, s)
        print("Checking custom name scrubbing...\t", end="", flush=True)
        text = ret['note'][0]['text']
        self.assertNotIn('John', text)
        self.assertNotIn('Smith', text)
        self.assertRegex(text, r'\[\[NAME_\d+\]\]')
        print(":thumbs_up:")

    def test_scrub_extract_names_from_resource(self):
        print("======== TEST SCRUB_TEXT EXTRACT NAMES ========")
        s = self._settings('Patient.note.text', patterns='all',
                           extra={'extract_names': True})
        resource = {
            'resourceType': 'Patient', 'id': 'p1',
            'name': [{'use': 'official', 'family': 'Pacocha', 'given': ['Earle']}],
            'note': [{'text': 'Patient Earle Pacocha reported dizziness.'}],
        }
        ret = process_data(resource, s)
        print("Checking auto name extraction...\t", end="", flush=True)
        text = ret['note'][0]['text']
        self.assertNotIn('Earle', text)
        self.assertNotIn('Pacocha', text)
        self.assertRegex(text, r'\[\[NAME_\d+\]\]')
        print(":thumbs_up:")

    def test_scrub_html_tokenize_mode(self):
        print("======== TEST SCRUB_TEXT HTML TOKENIZE MODE ========")
        original_div = (
            '<div xmlns="http://www.w3.org/1999/xhtml">'
            '<p>Patient John born 1980-01-01 seen today.</p>'
            '</div>'
        )
        s = self._settings('Patient.text', mode='html_tokenize',
                           patterns='date_iso', extra={'names': ['John']})
        resource = {'resourceType': 'Patient', 'id': 'p1',
                    'text': {'status': 'generated', 'div': original_div}}
        ret = process_data(resource, s)
        print("Checking HTML narrative tokenization...\t", end="", flush=True)
        div = ret['text']['div']
        self.assertNotIn('John', div)
        self.assertNotIn('1980-01-01', div)
        self.assertIn('<p>', div)
        self.assertIn('xmlns="http://www.w3.org/1999/xhtml"', div)
        self.assertRegex(div, r'\[\[NAME_\d+\]\]')
        self.assertRegex(div, r'\[\[DATE_\d+\]\]')
        print(":thumbs_up:")

    def test_scrub_multiple_notes(self):
        print("======== TEST SCRUB_TEXT MULTIPLE NOTES ========")
        s = self._settings('Condition.note.text', patterns='email,phone')
        resource = {
            'resourceType': 'Condition', 'id': 'c1',
            'note': [
                {'text': 'Email: patient@hospital.org'},
                {'text': 'Phone: 555-123-4567'},
                {'text': 'No PHI here.'},
            ],
        }
        ret = process_data(resource, s)
        print("Checking multiple notes scrubbing...\t", end="", flush=True)
        self.assertRegex(ret['note'][0]['text'], r'\[\[EMAIL_\d+\]\]')
        self.assertRegex(ret['note'][1]['text'], r'\[\[PHONE_\d+\]\]')
        self.assertEqual(ret['note'][2]['text'], 'No PHI here.')
        print(":thumbs_up:")

    def test_scrub_mrn(self):
        print("======== TEST SCRUB_TEXT MRN ========")
        s = self._settings('Observation.note.text', patterns='mrn')
        resource = {'resourceType': 'Observation', 'id': 'o1',
                    'note': [{'text': 'See chart MRN: 987654 for history.'}]}
        ret = process_data(resource, s)
        print("Checking MRN scrubbing...\t", end="", flush=True)
        self.assertNotIn('987654', ret['note'][0]['text'])
        self.assertRegex(ret['note'][0]['text'], r'\[\[MRN_\d+\]\]')
        print(":thumbs_up:")

    def test_scrub_all_patterns(self):
        print("======== TEST SCRUB_TEXT ALL PATTERNS ========")
        s = self._settings('Condition.note.text')
        rich_text = (
            'Patient DOB 1975-03-22, SSN 234-56-7890, '
            'phone (800) 555-0199, email doc@clinic.org, MRN: 100200.'
        )
        resource = {'resourceType': 'Condition', 'id': 'c1',
                    'note': [{'text': rich_text}]}
        ret = process_data(resource, s)
        text = ret['note'][0]['text']
        print("Checking all-patterns scrubbing...\t", end="", flush=True)
        self.assertNotIn('1975-03-22', text)
        self.assertNotIn('234-56-7890', text)
        self.assertNotIn('(800) 555-0199', text)
        self.assertNotIn('doc@clinic.org', text)
        self.assertNotIn('100200', text)
        print(":thumbs_up:")

    def test_scrub_nrp_fields(self):
        print("======== TEST SCRUB_TEXT NRP ========")
        s = self._settings('Observation.note.text', patterns='nationality,religion,political')
        resource = {
            'resourceType': 'Observation',
            'id': 'o1',
            'note': [{'text': 'nationality: German, religion: Muslim, political opinion: Green party'}],
        }
        ret = process_data(resource, s)
        text = ret['note'][0]['text']
        print("Checking NRP tokenization...\t", end="", flush=True)
        self.assertNotIn('German', text)
        self.assertNotIn('Muslim', text)
        self.assertNotIn('Green party', text)
        self.assertRegex(text, r'\[\[NATIONALITY_\d+\]\]')
        self.assertRegex(text, r'\[\[RELIGION_\d+\]\]')
        self.assertRegex(text, r'\[\[POLITICAL_\d+\]\]')
        print(":thumbs_up:")

    def test_scrub_account_and_national_id(self):
        print("======== TEST SCRUB_TEXT ACCOUNT/NID ========")
        s = self._settings('Condition.note.text', patterns='account,national_id')
        resource = {
            'resourceType': 'Condition',
            'id': 'c1',
            'note': [{'text': 'insurance member id: A12345Z and national id: DE-998877'}],
        }
        ret = process_data(resource, s)
        text = ret['note'][0]['text']
        print("Checking account and national-id tokenization...\t", end="", flush=True)
        self.assertNotIn('A12345Z', text)
        self.assertNotIn('DE-998877', text)
        self.assertRegex(text, r'\[\[ACCOUNT_\d+\]\]')
        self.assertRegex(text, r'\[\[NID_\d+\]\]')
        print(":thumbs_up:")

    def test_scrub_deterministic_token_same_value(self):
        print("======== TEST SCRUB_TEXT DETERMINISTIC TOKEN ========")
        s = self._settings('Condition.note.text', patterns='email')
        resource = {
            'resourceType': 'Condition',
            'id': 'c1',
            'note': [{'text': 'alice@example.org and again alice@example.org'}],
        }
        ret = process_data(resource, s)
        text = ret['note'][0]['text']
        print("Checking deterministic token mapping...\t", end="", flush=True)
        matches = list(re.findall(r'\[\[EMAIL_\d+\]\]', text))
        self.assertEqual(len(matches), 2)
        self.assertEqual(matches[0], matches[1])
        print(":thumbs_up:")


if __name__ == '__main__':
    unittest.main()
