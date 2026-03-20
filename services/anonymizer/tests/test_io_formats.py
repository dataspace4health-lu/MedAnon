import json
import tempfile
import unittest
from pathlib import Path

from pipeline.io_formats import (
    detect_format,
    read_fhir_xml,
    read_input_file,
    write_fhir_xml,
    write_output_file,
)


class TestIoFormats(unittest.TestCase):
    def test_detect_format_by_extension(self):
        self.assertEqual(detect_format('input.json', 'auto'), 'json')
        self.assertEqual(detect_format('input.ndjson', 'auto'), 'ndjson')
        self.assertEqual(detect_format('input.xml', 'auto'), 'xml')

    def test_xml_roundtrip_patient(self):
        patient = {
            'resourceType': 'Patient',
            'id': 'p1',
            'active': True,
            'name': [{'family': 'Doe', 'given': ['John']}],
            'birthDate': '1991-01-04',
        }
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'patient.xml'
            write_fhir_xml(patient, str(path))
            loaded = read_fhir_xml(str(path))

        self.assertEqual(loaded['resourceType'], 'Patient')
        self.assertEqual(loaded['id'], 'p1')
        self.assertTrue(loaded['active'])
        self.assertEqual(loaded['name'][0]['family'], 'Doe')
        self.assertEqual(loaded['name'][0]['given'][0], 'John')

    def test_ndjson_read_with_prefix(self):
        lines = [
            '//{"resourceType":"Patient","id":"a"}',
            '{"resourceType":"Patient","id":"b"}',
        ]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'in.ndjson'
            path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
            data = read_input_file(str(path), 'ndjson', strip_line_prefix='//')

        self.assertEqual(len(data), 2)
        self.assertEqual(data[0]['id'], 'a')
        self.assertEqual(data[1]['id'], 'b')

    def test_ndjson_write_list(self):
        payload = [
            {'resourceType': 'Patient', 'id': 'a'},
            {'resourceType': 'Patient', 'id': 'b'},
        ]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'out.ndjson'
            write_output_file(payload, str(path), 'ndjson')
            text = path.read_text(encoding='utf-8').strip().splitlines()

        self.assertEqual(len(text), 2)
        self.assertEqual(json.loads(text[0])['id'], 'a')
        self.assertEqual(json.loads(text[1])['id'], 'b')


if __name__ == '__main__':
    unittest.main()
