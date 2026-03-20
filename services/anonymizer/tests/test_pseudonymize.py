import unittest
from utils.fhirpath import read_resource_from_file
from pipeline.config import Settings
from pipeline.processor import process_data
import copy
from rich import print


class TestPseudonymize(unittest.TestCase):
    global infile
    infile = '../../../data/sample_fhir_data/simple_patient.json'

    def test_pseudonymize_encrypt_decrypt(self):
        print(f"=== TEST PSEUDONYMIZE/DEPSEUDONYMIZE ENCRYPT/DECRYPT ===")
        config_filename = 'test/config/encrypt.yaml'
        resource_filename = '../../../data/sample_fhir_data/simple_patient.json'
        resource = read_resource_from_file(resource_filename)
        settings = Settings(config_filename)
        original_resource = copy.deepcopy(resource)
        ret = process_data(resource, settings)
        config_filename = 'test/config/decrypt.yaml'
        settings = Settings(config_filename)
        ret2 = process_data(ret, settings)
        print(f"Checking encryption and decryption...\t", end="", flush=True)
        self.assertEqual(ret2['name'][0], original_resource['name'][0])
        self.assertEqual(ret2['name'][1], original_resource['name'][1])
        self.assertEqual(ret2['name'][2], original_resource['name'][2])
        print(f":thumbs_up:")


    def test_safe_harbor_redact(self):
        print(f"=== TEST SAFE HARBOR REDACT ===")
        config_filename = 'test/config/safe_harbor_redact.yaml'
        resource_filename = '../../../data/sample_fhir_data/patient_R5DB.json'
        resource = read_resource_from_file(resource_filename)
        settings = Settings(config_filename)
        ret = process_data(resource, settings)
        print(f"Checking redact...\t", end="", flush=True)
        self.assertRaises(KeyError, lambda: ret['name'])
        self.assertRaises(KeyError, lambda: ret['contact'][0]['name'])
        self.assertRaises(KeyError, lambda: ret['address'][0]['text'])
        self.assertRaises(KeyError, lambda: ret['address'][0]['line'])
        self.assertRaises(KeyError, lambda: ret['address'][0]['city'])
        self.assertRaises(KeyError, lambda: ret['address'][0]['district'])
        self.assertRaises(KeyError, lambda: ret['address'][0]['postalCode'])
        self.assertRaises(
            KeyError, lambda: ret['contact'][0]['address']['line'])
        self.assertRaises(
            KeyError, lambda: ret['contact'][0]['address']['city'])
        self.assertRaises(
            KeyError, lambda: ret['contact'][0]['address']['district'])
        self.assertRaises(
            KeyError, lambda: ret['contact'][0]['address']['postalCode'])
        self.assertRaises(KeyError, lambda: ret['birthDate'])
        self.assertRaises(
            KeyError, lambda: ret['_birthDate']['extension'][0]['valueDateTime'])
        self.assertRaises(
            KeyError, lambda: ret['address'][0]['period']['start'])
        self.assertRaises(
            KeyError, lambda: ret['contact'][0]['address']['period']['start'])
        self.assertRaises(KeyError, lambda: ret['telecom'][0]['value'])
        self.assertRaises(
            KeyError, lambda: ret['contact'][0]['telecom'][0]['value'])
        print(f":thumbs_up:")
