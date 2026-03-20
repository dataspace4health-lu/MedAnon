import unittest
import io
import os
from pipeline.config import Settings
from pipeline.processor import process_data
from utils.fhirpath import read_resource_from_file
from unittest.mock import patch
from urllib.error import HTTPError, URLError
import dateutil.parser as parser
from datetime import timedelta
import copy
import json
from rich import print


class TestProcessor(unittest.TestCase):
    """
    Testing settings applied to FHIR resources
    """

    def test_multiple_rules(self):
        print(f"======== TEST MULTIPLE RULES ========")
        config_filename = 'tests/config/multi_rule.yaml'
        resource_filename = 'tests/data/sample_fhir_data/patient_R5DB.json'
        resource = read_resource_from_file(resource_filename)
        settings = Settings(config_filename)
        original_resource = copy.deepcopy(resource)
        with patch.dict(os.environ, {'MEDANON_HASH_KEY': ''}, clear=False):
            ret = process_data(resource, settings)
        # Crypto hash checks
        print(f"Checking cryptohash...\t\t", end="", flush=True)
        self.assertEqual(
            ret['name'][0], 'c045ec1db0c14a237341851f8fae21edb1c7d4f36f1e7d49027b9c7a7e06c790')
        self.assertEqual(
            ret['name'][1], '6f4bae1f49ee29890cbfcf8ffb26eccc2520cb543fa30a28458e2952f40b7ea3')
        self.assertEqual(
            ret['name'][2], '96da330ddea4d222d7ae4b074da307520fb8ec00140682831b4b073a6110b8c0')
        print(f":thumbs_up:")
        # Perturb checks
        print(f"Checking perturb...\t\t", end="", flush=True)
        perturbed_date = parser.parse(ret['birthDate'])
        ref_date = parser.parse('1974-12-25')
        min_date = ref_date - timedelta(days=5)
        max_date = ref_date + timedelta(days=10)
        self.assertTrue(perturbed_date <= max_date)
        self.assertTrue(perturbed_date >= min_date)
        print(f":thumbs_up:")
        # Encrypt/Decrypt checks
        print(f"Checking encrypt/decrypt...\t", end="", flush=True)
        self.assertEqual(ret['address'][0], original_resource['address'][0])
        print(f":thumbs_up:")
        # Substitute checks
        print(f"Checking substitute...\t\t", end="", flush=True)
        self.assertEqual(ret['id'], 'foo')
        print(f":thumbs_up:")

    def test_returns_unmatched_resource_unchanged(self):
        print(f"======== TEST UNMATCHED RESOURCE ========")
        config_filename = 'config/config.yaml'
        resource_filename = 'tests/data/sample_fhir_data/simple_patient.json'
        resource = read_resource_from_file(resource_filename)
        settings = Settings(config_filename)

        ret = process_data(resource, settings)

        # Patient.name is redacted in default config — just verify resource type intact
        self.assertEqual(ret['resourceType'], 'Patient')
        # name should be removed (redacted), not preserved
        self.assertNotIn('name', ret)

    def test_process_bundle_entries(self):
        print(f"======== TEST BUNDLE PROCESSING ========")
        config_filename = 'config/config.yaml'
        resource = {
            'resourceType': 'Bundle',
            'type': 'transaction',
            'entry': [
                {
                    'resource': {
                        'resourceType': 'Patient',
                        'id': 'demo-patient',
                        'name': ['Alice', 'Example']
                    }
                },
                {
                    'resource': {
                        'resourceType': 'Observation',
                        'id': 'demo-observation',
                        'status': 'final'
                    }
                }
            ]
        }
        settings = Settings(config_filename)

        ret = process_data(resource, settings)

        self.assertEqual(ret['resourceType'], 'Bundle')
        # Patient.name is redacted in default config — the key should be gone
        self.assertNotIn('name', ret['entry'][0]['resource'])
        self.assertEqual(ret['entry'][1]['resource']['resourceType'], 'Observation')

    def test_wildcard_rule_applies_to_all_resource_types(self):
        print(f"======== TEST WILDCARD RULE ========")
        settings = type('SettingsObj', (), {
            'rules': [
                {
                    'match': '*.id',
                    'action': 'cryptohash',
                    'params': {'hash_type': 'sha3_256'}
                }
            ]
        })()

        resource = {
            'resourceType': 'Bundle',
            'entry': [
                {'resource': {'resourceType': 'Patient', 'id': 'p-1'}},
                {'resource': {'resourceType': 'Observation', 'id': 'o-1'}}
            ]
        }

        ret = process_data(resource, settings)

        self.assertNotEqual(ret['entry'][0]['resource']['id'], 'p-1')
        self.assertNotEqual(ret['entry'][1]['resource']['id'], 'o-1')

    def test_cryptohash_handles_identifier_value_lists(self):
        print(f"======== TEST CRYPTOHASH IDENTIFIER LIST ========")
        settings = type('S', (), {
            'rules': [{
                'match': 'Patient.identifier.value',
                'action': 'cryptohash',
                'params': {'hash_type': 'sha3_256'}
            }]
        })()

        resource = {
            'resourceType': 'Patient',
            'identifier': [
                {'system': 'urn:mrn', 'value': 'ABC123'},
                {'system': 'urn:ssn', 'value': '999-11-2222'}
            ]
        }

        ret = process_data(resource, settings)

        self.assertNotEqual(ret['identifier'][0]['value'], 'ABC123')
        self.assertNotEqual(ret['identifier'][1]['value'], '999-11-2222')
        self.assertEqual(len(ret['identifier'][0]['value']), 64)
        self.assertEqual(len(ret['identifier'][1]['value']), 64)

    @patch('integrations.gpas.client.request.urlopen')
    def test_gpas_pseudonymize_via_fhir_parameters(self, mock_urlopen):
        """gPAS $pseudonymizeAllowCreate: send original, receive pseudonym via FHIR Parameters."""
        print(f"======== TEST GPAS PSEUDONYMIZE (FHIR Parameters) ========")

        class _FakeResponse:
            def __init__(self, payload):
                self._payload = payload
                self.headers = type('H', (), {'get_content_charset': lambda self, default='utf-8': 'utf-8'})()
            def read(self):
                return self._payload
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        # Simulate gPAS response: Parameters with pseudonym mapping
        gpas_response = {
            "resourceType": "Parameters",
            "parameter": [
                {
                    "name": "pseudonym",
                    "part": [
                        {"name": "target", "valueIdentifier": {"system": "https://ths-greifswald.de/gpas", "value": "TESTDOMAIN"}},
                        {"name": "original", "valueIdentifier": {"system": "https://ths-greifswald.de/gpas", "value": "patient-123"}},
                        {"name": "pseudonym", "valueIdentifier": {"system": "https://ths-greifswald.de/gpas", "value": "psn_ABCDEF01"}}
                    ]
                }
            ]
        }
        mock_urlopen.return_value = _FakeResponse(json.dumps(gpas_response).encode('utf-8'))

        settings = type('S', (), {
            'rules': [{
                'match': 'Patient.id',
                'action': 'gpas_pseudonymize',
                'params': {
                    'gpas_url': 'https://demo.ths-greifswald.de/ttp-fhir/fhir/gpas',
                    'gpas_domain': 'TESTDOMAIN',
                    'gpas_cache_enabled': False,
                }
            }]
        })()

        resource = {'resourceType': 'Patient', 'id': 'patient-123', 'name': [{'family': 'Smith'}]}
        ret = process_data(resource, settings)

        # id is replaced with gPAS pseudonym, name is untouched
        self.assertEqual(ret['id'], 'psn_ABCDEF01')
        self.assertEqual(ret['name'][0]['family'], 'Smith')

        # Verify the HTTP call was made to the correct $pseudonymizeAllowCreate URL
        actual_req = mock_urlopen.call_args[0][0]
        self.assertIn('/$pseudonymizeAllowCreate', actual_req.full_url)
        sent_body = json.loads(actual_req.data)
        self.assertEqual(sent_body['resourceType'], 'Parameters')
        # Should contain target + original parameters
        names = [p['name'] for p in sent_body['parameter']]
        self.assertIn('target', names)
        self.assertIn('original', names)
        print(f"Checking gPAS pseudonymize (FHIR Parameters)...\t:thumbs_up:")

    @patch('integrations.gpas.client.request.urlopen')
    def test_gpas_depseudonymize_via_fhir_parameters(self, mock_urlopen):
        """gPAS $dePseudonymize: send pseudonym, receive original via FHIR Parameters."""
        print(f"======== TEST GPAS DEPSEUDONYMIZE (FHIR Parameters) ========")

        class _FakeResponse:
            def __init__(self, payload):
                self._payload = payload
                self.headers = type('H', (), {'get_content_charset': lambda self, default='utf-8': 'utf-8'})()
            def read(self):
                return self._payload
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        gpas_response = {
            "resourceType": "Parameters",
            "parameter": [
                {
                    "name": "original",
                    "part": [
                        {"name": "target", "valueIdentifier": {"system": "https://ths-greifswald.de/gpas", "value": "TESTDOMAIN"}},
                        {"name": "original", "valueIdentifier": {"system": "https://ths-greifswald.de/gpas", "value": "patient-123"}},
                        {"name": "pseudonym", "valueIdentifier": {"system": "https://ths-greifswald.de/gpas", "value": "psn_ABCDEF01"}}
                    ]
                }
            ]
        }
        mock_urlopen.return_value = _FakeResponse(json.dumps(gpas_response).encode('utf-8'))

        settings = type('S', (), {
            'rules': [{
                'match': 'Patient.id',
                'action': 'gpas_depseudonymize',
                'params': {
                    'gpas_url': 'https://demo.ths-greifswald.de/ttp-fhir/fhir/gpas',
                    'gpas_domain': 'TESTDOMAIN',
                    'gpas_cache_enabled': False,
                }
            }]
        })()

        resource = {'resourceType': 'Patient', 'id': 'psn_ABCDEF01', 'name': [{'family': 'Smith'}]}
        ret = process_data(resource, settings)

        self.assertEqual(ret['id'], 'patient-123')
        self.assertEqual(ret['name'][0]['family'], 'Smith')

        actual_req = mock_urlopen.call_args[0][0]
        self.assertIn('/$dePseudonymize', actual_req.full_url)
        sent_body = json.loads(actual_req.data)
        names = [p['name'] for p in sent_body['parameter']]
        self.assertIn('target', names)
        self.assertIn('pseudonym', names)
        print(f"Checking gPAS depseudonymize (FHIR Parameters)...\t:thumbs_up:")

    @patch('integrations.gpas.client.request.urlopen')
    def test_gpas_error_handling(self, mock_urlopen):
        """gPAS returns an error entry when an original value is not found."""
        print(f"======== TEST GPAS ERROR HANDLING ========")

        class _FakeResponse:
            def __init__(self, payload):
                self._payload = payload
                self.headers = type('H', (), {'get_content_charset': lambda self, default='utf-8': 'utf-8'})()
            def read(self):
                return self._payload
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        gpas_error_response = {
            "resourceType": "Parameters",
            "parameter": [
                {
                    "name": "error",
                    "part": [
                        {"name": "original", "valueIdentifier": {"system": "https://ths-greifswald.de/gpas", "value": "unknown-id"}},
                        {"name": "error-code", "valueCoding": {"system": "http://hl7.org/fhir/issue-type", "code": "not-found", "display": "Not Found"}}
                    ]
                }
            ]
        }
        mock_urlopen.return_value = _FakeResponse(json.dumps(gpas_error_response).encode('utf-8'))

        settings = type('S', (), {
            'rules': [{
                'match': 'Patient.id',
                'action': 'gpas_pseudonymize',
                'params': {
                    'gpas_url': 'https://demo.ths-greifswald.de/ttp-fhir/fhir/gpas',
                    'gpas_domain': 'TESTDOMAIN',
                    'gpas_operation': 'pseudonymize',
                    'gpas_cache_enabled': False,
                }
            }]
        })()

        resource = {'resourceType': 'Patient', 'id': 'unknown-demo.ths-greifswald.deid'}
        with self.assertRaises(ValueError) as ctx:
            process_data(resource, settings)
        self.assertIn('not-found', str(ctx.exception))
        print(f"Checking gPAS error handling...\t\t:thumbs_up:")

    @patch('integrations.gpas.client.request.urlopen')
    def test_gpas_admin_url_is_normalized_to_fhir_base(self, mock_urlopen):
        """A gPAS admin UI URL should be normalized to the FHIR API base."""
        print(f"======== TEST GPAS ADMIN URL NORMALIZATION ========")

        class _FakeResponse:
            def __init__(self, payload, content_type='application/fhir+json'):
                self._payload = payload
                self.headers = type('H', (), {
                    'get_content_charset': lambda self, default='utf-8': 'utf-8'
                })()
                self.content_type = content_type
            def read(self):
                return self._payload
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        gpas_response = {
            "resourceType": "Parameters",
            "parameter": [
                {
                    "name": "pseudonym",
                    "part": [
                        {"name": "target", "valueIdentifier": {"system": "https://ths-greifswald.de/gpas", "value": "demo.study.demo"}},
                        {"name": "original", "valueIdentifier": {"system": "https://ths-greifswald.de/gpas", "value": "patient-123"}},
                        {"name": "pseudonym", "valueIdentifier": {"system": "https://ths-greifswald.de/gpas", "value": "demo_123"}}
                    ]
                }
            ]
        }
        mock_urlopen.return_value = _FakeResponse(json.dumps(gpas_response).encode('utf-8'))

        settings = type('S', (), {
            'rules': [{
                'match': 'Patient.id',
                'action': 'gpas_pseudonymize',
                'params': {
                    'gpas_url': 'http://10.168.192.22:8080/gpas-web/html/internal/admin/export.xhtml',
                    'gpas_domain': 'demo.study.demo',
                    'gpas_cache_enabled': False,
                }
            }]
        })()

        resource = {'resourceType': 'Patient', 'id': 'patient-123'}
        ret = process_data(resource, settings)

        self.assertEqual(ret['id'], 'demo_123')
        actual_req = mock_urlopen.call_args[0][0]
        self.assertEqual(actual_req.full_url, 'http://10.168.192.22:8080/ttp-fhir/fhir/gpas/$pseudonymizeAllowCreate')
        print(f"Checking gPAS admin URL normalization...\t:thumbs_up:")

    @patch('integrations.gpas.client.request.urlopen')
    def test_gpas_unknown_domain_lists_available_domains(self, mock_urlopen):
        """Unknown-domain errors should include domains discovered from the admin UI."""
        print(f"======== TEST GPAS DOMAIN DISCOVERY ========")

        class _FakeResponse:
            def __init__(self, payload, content_type='text/html'):
                self._payload = payload
                self.headers = type('H', (), {
                    'get_content_charset': lambda self, default='utf-8': 'utf-8'
                })()
                self.content_type = content_type
            def read(self):
                return self._payload
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        html_payload = b'<li class="ui-picklist-item" data-item-label="demo.study.demo"></li><li class="ui-picklist-item" data-item-label="Biolabor"></li>'
        error_payload = io.BytesIO(json.dumps({
            'resourceType': 'OperationOutcome',
            'issue': [{'diagnostics': "Unknown domain 'TESTDOMAIN'."}]
        }).encode('utf-8'))

        def _fake_urlopen(req, timeout=30):
            url = req.full_url if hasattr(req, 'full_url') else req
            if 'export.xhtml' in url:
                return _FakeResponse(html_payload)
            raise HTTPError(url, 400, 'Bad Request', hdrs=None, fp=error_payload)

        mock_urlopen.side_effect = _fake_urlopen

        settings = type('S', (), {
            'rules': [{
                'match': 'Patient.id',
                'action': 'gpas_pseudonymize',
                'params': {
                    'gpas_url': 'http://10.168.192.22:8080/ttp-fhir/fhir/gpas',
                    'gpas_domain': 'demo.study.demo',
                    'gpas_cache_enabled': False,
                }
            }]
        })()

        resource = {'resourceType': 'Patient', 'id': 'patient-123'}
        with self.assertRaises(ValueError) as ctx:
            process_data(resource, settings)
        self.assertIn('Available domains: Biolabor, demo.study.demo', str(ctx.exception))
        print(f"Checking gPAS domain discovery...\t\t:thumbs_up:")

    @patch('integrations.gpas.client.request.urlopen')
    def test_bundle_reference_rewriting_after_pseudonymization(self, mock_urlopen):
        """When resource IDs are pseudonymized, all FHIR references and request.url must follow."""
        print(f"======== TEST BUNDLE REFERENCE REWRITING ========")

        _call_counter = [0]
        _pseudo_map = {
            'pat-1': 'psn_AAA',
            'obs-1': 'psn_BBB',
        }

        class _FakeResponse:
            def __init__(self, payload):
                self._payload = payload
                self.headers = type('H', (), {'get_content_charset': lambda self, default='utf-8': 'utf-8'})()
            def read(self):
                return self._payload
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        def _fake_urlopen(req, timeout=30):
            _call_counter[0] += 1
            sent = json.loads(req.data)
            original_value = None
            for p in sent['parameter']:
                if p['name'] == 'original':
                    original_value = p['valueString']
            pseudonym = _pseudo_map.get(original_value, f'psn_{original_value}')
            resp = {
                "resourceType": "Parameters",
                "parameter": [{
                    "name": "pseudonym",
                    "part": [
                        {"name": "target", "valueIdentifier": {"system": "https://ths-greifswald.de/gpas", "value": "TESTDOMAIN"}},
                        {"name": "original", "valueIdentifier": {"system": "https://ths-greifswald.de/gpas", "value": original_value}},
                        {"name": "pseudonym", "valueIdentifier": {"system": "https://ths-greifswald.de/gpas", "value": pseudonym}},
                    ]
                }]
            }
            return _FakeResponse(json.dumps(resp).encode('utf-8'))

        mock_urlopen.side_effect = _fake_urlopen

        settings = type('S', (), {
            'rules': [{
                'match': '*.id',
                'action': 'gpas_pseudonymize',
                'params': {
                    'gpas_url': 'https://example.com/ttp-fhir/fhir/gpas',
                    'gpas_domain': 'TESTDOMAIN',
                    'gpas_cache_enabled': False,
                }
            }]
        })()

        bundle = {
            'resourceType': 'Bundle',
            'type': 'transaction',
            'entry': [
                {
                    'resource': {
                        'resourceType': 'Patient',
                        'id': 'pat-1',
                        'name': [{'family': 'Smith'}],
                    },
                    'request': {'method': 'PUT', 'url': 'Patient/pat-1'}
                },
                {
                    'resource': {
                        'resourceType': 'Observation',
                        'id': 'obs-1',
                        'status': 'final',
                        'subject': {'reference': 'Patient/pat-1'},
                    },
                    'request': {'method': 'PUT', 'url': 'Observation/obs-1'}
                },
            ]
        }

        ret = process_data(bundle, settings)

        # Resource IDs are pseudonymized
        self.assertEqual(ret['entry'][0]['resource']['id'], 'psn_AAA')
        self.assertEqual(ret['entry'][1]['resource']['id'], 'psn_BBB')

        # request.url rewritten to match new IDs
        self.assertEqual(ret['entry'][0]['request']['url'], 'Patient/psn_AAA')
        self.assertEqual(ret['entry'][1]['request']['url'], 'Observation/psn_BBB')

        # Cross-reference from Observation -> Patient is rewritten
        self.assertEqual(ret['entry'][1]['resource']['subject']['reference'], 'Patient/psn_AAA')

        # Name untouched (no rule for it)
        self.assertEqual(ret['entry'][0]['resource']['name'][0]['family'], 'Smith')

        # Two gPAS calls were made (one per resource)
        self.assertEqual(_call_counter[0], 2)

        print(f"Checking bundle reference rewriting...\t\t:thumbs_up:")

    def test_processing_errors_skip_policy(self):
        print(f"======== TEST PROCESSING ERRORS SKIP ========")
        settings = type('S', (), {
            'rules': [{'match': 'Patient.id', 'action': 'unknown_action'}],
            'processing_errors': 'skip',
        })()
        resource = {'resourceType': 'Patient', 'id': 'abc'}
        ret = process_data(resource, settings)
        # Step 4: failed actions now redact the field to prevent PHI leakage
        self.assertNotIn('id', ret)
        print(f"Checking processing error skip policy...\t:thumbs_up:")

    def test_dynamic_rule_settings_interpolation(self):
        print(f"======== TEST DYNAMIC RULE SETTINGS ========")
        settings = type('S', (), {
            'rules': [{
                'match': 'Patient.id',
                'action': 'substitute',
                'params': {'substitute_with': '{{replacement}}'},
            }],
            'dynamic_rule_settings': {'replacement': 'dyn-123'},
        })()
        resource = {'resourceType': 'Patient', 'id': 'abc'}
        ret = process_data(resource, settings)
        self.assertEqual(ret['id'], 'dyn-123')
        print(f"Checking dynamic settings interpolation...\t:thumbs_up:")

    @patch('integrations.gpas.client.request.urlopen')
    def test_gpas_retry_on_transient_urlerror(self, mock_urlopen):
        print(f"======== TEST GPAS RETRY ========")

        class _FakeResponse:
            def __init__(self, payload):
                self._payload = payload
                self.headers = type('H', (), {'get_content_charset': lambda self, default='utf-8': 'utf-8'})()
            def read(self):
                return self._payload
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        success = {
            "resourceType": "Parameters",
            "parameter": [{
                "name": "pseudonym",
                "part": [
                    {"name": "original", "valueIdentifier": {"value": "patient-1"}},
                    {"name": "pseudonym", "valueIdentifier": {"value": "psn_1"}},
                ]
            }]
        }
        mock_urlopen.side_effect = [
            URLError('temporary network issue'),
            _FakeResponse(json.dumps(success).encode('utf-8')),
        ]

        settings = type('S', (), {
            'rules': [{
                'match': 'Patient.id',
                'action': 'gpas_pseudonymize',
                'params': {
                    'gpas_url': 'https://example.org/ttp-fhir/fhir/gpas',
                    'gpas_domain': 'TEST',
                    'gpas_retry_count': 1,
                    'gpas_cache_enabled': False,
                }
            }]
        })()

        resource = {'resourceType': 'Patient', 'id': 'patient-1'}
        ret = process_data(resource, settings)
        self.assertEqual(ret['id'], 'psn_1')
        self.assertEqual(mock_urlopen.call_count, 2)
        print(f"Checking gPAS retry behavior...\t\t:thumbs_up:")

    @patch('integrations.gpas.client.request.urlopen')
    def test_gpas_cache_avoids_duplicate_calls(self, mock_urlopen):
        print(f"======== TEST GPAS CACHE ========")

        class _FakeResponse:
            def __init__(self, payload):
                self._payload = payload
                self.headers = type('H', (), {'get_content_charset': lambda self, default='utf-8': 'utf-8'})()
            def read(self):
                return self._payload
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        def _fake_urlopen(req, timeout=30):
            sent = json.loads(req.data)
            original = [p['valueString'] for p in sent['parameter'] if p['name'] == 'original'][0]
            resp = {
                "resourceType": "Parameters",
                "parameter": [{
                    "name": "pseudonym",
                    "part": [
                        {"name": "original", "valueIdentifier": {"value": original}},
                        {"name": "pseudonym", "valueIdentifier": {"value": "psn_cached"}},
                    ]
                }]
            }
            return _FakeResponse(json.dumps(resp).encode('utf-8'))

        mock_urlopen.side_effect = _fake_urlopen

        settings = type('S', (), {
            'rules': [{
                'match': 'Patient.id',
                'action': 'gpas_pseudonymize',
                'params': {
                    'gpas_url': 'https://example.org/ttp-fhir/fhir/gpas',
                    'gpas_domain': 'TEST',
                    'gpas_cache_enabled': True,
                }
            }]
        })()

        resources = [
            {'resourceType': 'Patient', 'id': 'same-id'},
            {'resourceType': 'Patient', 'id': 'same-id'},
        ]
        ret = process_data(resources, settings)
        self.assertEqual(ret[0]['id'], 'psn_cached')
        self.assertEqual(ret[1]['id'], 'psn_cached')
        self.assertEqual(mock_urlopen.call_count, 1)
        print(f"Checking gPAS cache behavior...\t\t:thumbs_up:")

    


if __name__ == '__main__':
    unittest.main()
