"""Tests for the CLI module (cli/main.py).

Covers: process (JSON/NDJSON), fetch, everything, push subcommands.
Runs locally -- no Docker required. External dependencies are mocked.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from cli.main import main


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SIMPLE_PATIENT = {
    "resourceType": "Patient",
    "id": "test-1",
    "name": [{"family": "Doe", "given": ["John"]}],
    "birthDate": "1990-05-15",
}

_PROCESSED_PATIENT = {
    "resourceType": "Patient",
    "id": "hashed-test-1",
}

_SIMPLE_OBSERVATION = {
    "resourceType": "Observation",
    "id": "obs-1",
    "status": "final",
    "code": {"coding": [{"system": "http://loinc.org", "code": "1234-5"}]},
}


# ===========================================================================
# TestProcess — default process subcommand
# ===========================================================================

class TestProcess(unittest.TestCase):
    """Tests for the process (default) subcommand: JSON/NDJSON file I/O."""

    @patch("pipeline.config.Settings")
    @patch("cli.main.process_data")
    def test_json_file_in_processed_out(self, mock_process, mock_settings):
        """JSON input file is processed and written to JSON output file."""
        mock_process.return_value = _PROCESSED_PATIENT
        mock_settings.return_value = MagicMock()

        with tempfile.TemporaryDirectory() as d:
            input_path = Path(d) / "input.json"
            output_path = Path(d) / "output.json"
            input_path.write_text(json.dumps(_SIMPLE_PATIENT), encoding="utf-8")

            main(["process", str(input_path), str(output_path),
                  "--config", "dummy.yaml"])

            mock_settings.assert_called_once_with("dummy.yaml")
            mock_process.assert_called_once()
            self.assertTrue(output_path.exists())
            result = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(result["resourceType"], "Patient")
            self.assertEqual(result["id"], "hashed-test-1")

    @patch("pipeline.config.Settings")
    @patch("cli.main.process_data")
    def test_ndjson_streaming(self, mock_process, mock_settings):
        """NDJSON input is streamed line-by-line through process_data."""
        mock_process.side_effect = lambda res, _settings: {
            "resourceType": res["resourceType"],
            "id": "processed-" + res["id"],
        }
        mock_settings.return_value = MagicMock()

        with tempfile.TemporaryDirectory() as d:
            input_path = Path(d) / "input.ndjson"
            output_path = Path(d) / "output.ndjson"
            lines = [
                json.dumps(_SIMPLE_PATIENT),
                json.dumps(_SIMPLE_OBSERVATION),
            ]
            input_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

            main([str(input_path), str(output_path), "--config", "dummy.yaml"])

            self.assertEqual(mock_process.call_count, 2)
            self.assertTrue(output_path.exists())
            result_lines = [
                json.loads(line)
                for line in output_path.read_text(encoding="utf-8").strip().splitlines()
            ]
            self.assertEqual(len(result_lines), 2)
            self.assertEqual(result_lines[0]["id"], "processed-test-1")
            self.assertEqual(result_lines[1]["id"], "processed-obs-1")

    @patch("pipeline.config.Settings")
    @patch("cli.main.process_data")
    def test_ndjson_skips_blank_lines(self, mock_process, mock_settings):
        """NDJSON streaming skips blank lines without errors."""
        mock_process.side_effect = lambda res, _s: res
        mock_settings.return_value = MagicMock()

        with tempfile.TemporaryDirectory() as d:
            input_path = Path(d) / "input.ndjson"
            output_path = Path(d) / "output.ndjson"
            content = json.dumps(_SIMPLE_PATIENT) + "\n\n\n" + json.dumps(_SIMPLE_OBSERVATION) + "\n"
            input_path.write_text(content, encoding="utf-8")

            main([str(input_path), str(output_path), "--config", "dummy.yaml"])

            self.assertEqual(mock_process.call_count, 2)

    @patch("pipeline.config.Settings")
    @patch("cli.main.process_data")
    def test_config_flag_passes_settings(self, mock_process, mock_settings):
        """--config flag creates Settings and passes it to process_data."""
        mock_process.return_value = _PROCESSED_PATIENT
        sentinel = MagicMock()
        mock_settings.return_value = sentinel

        with tempfile.TemporaryDirectory() as d:
            input_path = Path(d) / "input.json"
            output_path = Path(d) / "output.json"
            input_path.write_text(json.dumps(_SIMPLE_PATIENT), encoding="utf-8")

            main([str(input_path), str(output_path), "--config", "/path/to/rules.yaml"])

            mock_settings.assert_called_once_with("/path/to/rules.yaml")
            # Verify the settings object was passed to process_data
            args, _ = mock_process.call_args
            self.assertIs(args[1], sentinel)

    @patch("pipeline.config.Settings")
    @patch("cli.main.process_data")
    def test_missing_input_file_raises(self, mock_process, mock_settings):
        """Referencing a non-existent input file raises FileNotFoundError."""
        mock_settings.return_value = MagicMock()

        with tempfile.TemporaryDirectory() as d:
            output_path = Path(d) / "output.json"
            with self.assertRaises(FileNotFoundError):
                main(["/nonexistent/input.json", str(output_path),
                      "--config", "dummy.yaml"])

    def test_no_positional_args_exits(self):
        """No positional args triggers argparse error -> SystemExit(2)."""
        with self.assertRaises(SystemExit) as ctx:
            main(["--config", "dummy.yaml"])
        self.assertEqual(ctx.exception.code, 2)

    @patch("pipeline.config.Settings")
    @patch("cli.main.process_data")
    def test_process_prefix_stripped_from_argv(self, mock_process, mock_settings):
        """Leading 'process' token in argv is transparently stripped."""
        mock_process.return_value = _PROCESSED_PATIENT
        mock_settings.return_value = MagicMock()

        with tempfile.TemporaryDirectory() as d:
            input_path = Path(d) / "input.json"
            output_path = Path(d) / "output.json"
            input_path.write_text(json.dumps(_SIMPLE_PATIENT), encoding="utf-8")

            # Both with and without "process" prefix should work identically
            main(["process", str(input_path), str(output_path), "--config", "d.yaml"])
            first_call_args = mock_process.call_args

            mock_process.reset_mock()
            mock_settings.reset_mock()
            mock_process.return_value = _PROCESSED_PATIENT
            mock_settings.return_value = MagicMock()

            main([str(input_path), str(output_path), "--config", "d.yaml"])
            second_call_args = mock_process.call_args

            # The resource passed to process_data should be the same in both cases
            self.assertEqual(first_call_args[0][0], second_call_args[0][0])


# ===========================================================================
# TestFetch — fetch subcommand
# ===========================================================================

class TestFetch(unittest.TestCase):
    """Tests for the fetch subcommand: FHIR resource download."""

    @patch("cli.main.get_capability_statement")
    @patch("cli.main.fetch_all_resource_types")
    def test_fetch_calls_fhir_client(self, mock_fetch_all, mock_cap):
        """fetch discovers types via /metadata and writes NDJSON output."""
        mock_cap.return_value = ["Patient"]
        mock_fetch_all.return_value = iter([
            ("Patient", _SIMPLE_PATIENT),
            ("Patient", {**_SIMPLE_PATIENT, "id": "test-2"}),
        ])

        with tempfile.TemporaryDirectory() as d:
            output_path = Path(d) / "output.ndjson"
            main(["fetch", "--server", "http://fhir.example.com/fhir",
                  "--output", str(output_path)])

            mock_cap.assert_called_once()
            mock_fetch_all.assert_called_once()
            self.assertTrue(output_path.exists())
            lines = output_path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[0])["id"], "test-1")
            self.assertEqual(json.loads(lines[1])["id"], "test-2")

    @patch("cli.main.get_capability_statement")
    @patch("cli.main.fetch_all_resource_types")
    def test_fetch_server_flag(self, mock_fetch_all, mock_cap):
        """--server URL is forwarded to fetch_all_resource_types."""
        mock_cap.return_value = ["Patient"]
        mock_fetch_all.return_value = iter([("Patient", _SIMPLE_PATIENT)])

        with tempfile.TemporaryDirectory() as d:
            output_path = Path(d) / "output.ndjson"
            main(["fetch", "--server", "http://custom:8080/fhir",
                  "--output", str(output_path)])

            args, kwargs = mock_fetch_all.call_args
            self.assertEqual(args[0], "http://custom:8080/fhir")

    @patch("cli.main.fetch_all_resource_types")
    def test_fetch_explicit_resource_type(self, mock_fetch_all):
        """--resource-type bypasses /metadata discovery."""
        mock_fetch_all.return_value = iter([("Observation", _SIMPLE_OBSERVATION)])

        with tempfile.TemporaryDirectory() as d:
            output_path = Path(d) / "output.ndjson"
            main(["fetch", "--server", "http://fhir.example.com/fhir",
                  "--resource-type", "Observation",
                  "--output", str(output_path)])

            mock_fetch_all.assert_called_once()
            args, kwargs = mock_fetch_all.call_args
            # Second positional arg is the list of resource types
            self.assertEqual(args[1], ["Observation"])

    @patch("cli.main.get_capability_statement")
    def test_fetch_discover_only(self, mock_cap):
        """--discover-only calls get_capability_statement and returns."""
        mock_cap.return_value = ["Patient", "Observation", "Condition"]

        with tempfile.TemporaryDirectory() as d:
            output_path = Path(d) / "output.ndjson"
            main(["fetch", "--server", "http://fhir.example.com/fhir",
                  "--output", str(output_path), "--discover-only"])

            mock_cap.assert_called_once_with(
                "http://fhir.example.com/fhir", token=None, timeout=30.0,
            )
            # Discover writes types to the output file
            content = output_path.read_text()
            self.assertIn("Patient", content)
            self.assertIn("Observation", content)
            self.assertIn("Condition", content)

    @patch.dict(os.environ, {"FHIR_SOURCE_URL": ""}, clear=False)
    def test_fetch_no_server_exits(self):
        """fetch without --server and empty FHIR_SOURCE_URL -> SystemExit."""
        with tempfile.TemporaryDirectory() as d:
            output_path = Path(d) / "output.ndjson"
            with self.assertRaises(SystemExit) as ctx:
                main(["fetch", "--output", str(output_path)])
            self.assertIn("--server", str(ctx.exception))

    def test_fetch_missing_output_exits(self):
        """fetch without required --output -> SystemExit(2) from argparse."""
        with self.assertRaises(SystemExit) as ctx:
            main(["fetch", "--server", "http://fhir.example.com/fhir"])
        self.assertEqual(ctx.exception.code, 2)


# ===========================================================================
# TestEverything — everything subcommand
# ===========================================================================

class TestEverything(unittest.TestCase):
    """Tests for the everything subcommand: FHIR $everything operation."""

    @patch("cli.main.fetch_everything")
    def test_everything_calls_fetch_everything(self, mock_fetch_ev):
        """everything subcommand calls fetch_everything with correct args."""
        mock_fetch_ev.return_value = iter([
            _SIMPLE_PATIENT,
            _SIMPLE_OBSERVATION,
        ])

        with tempfile.TemporaryDirectory() as d:
            output_path = Path(d) / "output.ndjson"
            main([
                "everything",
                "--server", "http://fhir.example.com/fhir",
                "--resource-type", "Patient",
                "--id", "DDME",
                "--output", str(output_path),
            ])

            mock_fetch_ev.assert_called_once()
            args, kwargs = mock_fetch_ev.call_args
            self.assertEqual(args[0], "http://fhir.example.com/fhir")
            self.assertEqual(args[1], "Patient")
            self.assertEqual(args[2], "DDME")
            self.assertTrue(output_path.exists())
            lines = output_path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 2)

    @patch("cli.main.fetch_everything")
    def test_everything_writes_ndjson(self, mock_fetch_ev):
        """Output file contains one JSON object per line."""
        mock_fetch_ev.return_value = iter([_SIMPLE_PATIENT])

        with tempfile.TemporaryDirectory() as d:
            output_path = Path(d) / "output.ndjson"
            main([
                "everything",
                "--server", "http://fhir.example.com/fhir",
                "--resource-type", "Patient",
                "--id", "P1",
                "--output", str(output_path),
            ])

            content = output_path.read_text(encoding="utf-8").strip()
            parsed = json.loads(content)
            self.assertEqual(parsed["resourceType"], "Patient")
            self.assertEqual(parsed["id"], "test-1")

    def test_everything_requires_resource_type(self):
        """Omitting --resource-type triggers argparse error -> SystemExit(2)."""
        with tempfile.TemporaryDirectory() as d:
            output_path = Path(d) / "output.ndjson"
            with self.assertRaises(SystemExit) as ctx:
                main([
                    "everything",
                    "--server", "http://fhir.example.com/fhir",
                    "--id", "DDME",
                    "--output", str(output_path),
                ])
            self.assertEqual(ctx.exception.code, 2)

    def test_everything_requires_id(self):
        """Omitting --id triggers argparse error -> SystemExit(2)."""
        with tempfile.TemporaryDirectory() as d:
            output_path = Path(d) / "output.ndjson"
            with self.assertRaises(SystemExit) as ctx:
                main([
                    "everything",
                    "--server", "http://fhir.example.com/fhir",
                    "--resource-type", "Patient",
                    "--output", str(output_path),
                ])
            self.assertEqual(ctx.exception.code, 2)

    def test_everything_requires_output(self):
        """Omitting --output triggers argparse error -> SystemExit(2)."""
        with self.assertRaises(SystemExit) as ctx:
            main([
                "everything",
                "--server", "http://fhir.example.com/fhir",
                "--resource-type", "Patient",
                "--id", "DDME",
            ])
        self.assertEqual(ctx.exception.code, 2)

    @patch.dict(os.environ, {"FHIR_SOURCE_URL": ""}, clear=False)
    def test_everything_no_server_exits(self):
        """everything without --server and empty env -> SystemExit."""
        with tempfile.TemporaryDirectory() as d:
            output_path = Path(d) / "output.ndjson"
            with self.assertRaises(SystemExit) as ctx:
                main([
                    "everything",
                    "--resource-type", "Patient",
                    "--id", "DDME",
                    "--output", str(output_path),
                ])
            self.assertIn("--server", str(ctx.exception))


# ===========================================================================
# TestPush — push subcommand
# ===========================================================================

class TestPush(unittest.TestCase):
    """Tests for the push subcommand: upload resources to a FHIR server."""

    @patch("cli.main.upload_resources")
    def test_push_reads_file_and_uploads(self, mock_upload):
        """push reads a JSON file and uploads its resources."""
        captured_resources = []

        def fake_upload(server, resources_gen, **kwargs):
            for res in resources_gen:
                captured_resources.append(res)
                yield {
                    "resourceType": res["resourceType"],
                    "source_id": res.get("id"),
                    "server_id": res.get("id"),
                    "success": True,
                    "error": None,
                }

        mock_upload.side_effect = fake_upload

        with tempfile.TemporaryDirectory() as d:
            input_path = Path(d) / "input.json"
            input_path.write_text(json.dumps(_SIMPLE_PATIENT), encoding="utf-8")

            main([
                "push",
                "--server", "http://target:8080/fhir",
                "--input", str(input_path),
            ])

            mock_upload.assert_called_once()
            self.assertEqual(len(captured_resources), 1)
            self.assertEqual(captured_resources[0]["resourceType"], "Patient")
            self.assertEqual(captured_resources[0]["id"], "test-1")

    @patch("cli.main.upload_resources")
    def test_push_ndjson_file(self, mock_upload):
        """push reads an NDJSON file and uploads multiple resources."""
        captured_resources = []

        def fake_upload(server, resources_gen, **kwargs):
            for res in resources_gen:
                captured_resources.append(res)
                yield {
                    "resourceType": res["resourceType"],
                    "source_id": res.get("id"),
                    "server_id": res.get("id"),
                    "success": True,
                    "error": None,
                }

        mock_upload.side_effect = fake_upload

        with tempfile.TemporaryDirectory() as d:
            input_path = Path(d) / "input.ndjson"
            lines = [json.dumps(_SIMPLE_PATIENT), json.dumps(_SIMPLE_OBSERVATION)]
            input_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

            main([
                "push",
                "--server", "http://target:8080/fhir",
                "--input", str(input_path),
            ])

            self.assertEqual(len(captured_resources), 2)
            self.assertEqual(captured_resources[0]["id"], "test-1")
            self.assertEqual(captured_resources[1]["id"], "obs-1")

    @patch("cli.main.upload_resources")
    def test_push_server_flag(self, mock_upload):
        """--server URL is forwarded to upload_resources."""
        mock_upload.return_value = iter([
            {"resourceType": "Patient", "source_id": "test-1",
             "server_id": "test-1", "success": True, "error": None},
        ])

        with tempfile.TemporaryDirectory() as d:
            input_path = Path(d) / "input.json"
            input_path.write_text(json.dumps(_SIMPLE_PATIENT), encoding="utf-8")

            main([
                "push",
                "--server", "http://custom:9090/fhir",
                "--input", str(input_path),
            ])

            args, kwargs = mock_upload.call_args
            self.assertEqual(args[0], "http://custom:9090/fhir")

    @patch("cli.main.upload_resources")
    def test_push_handles_errors(self, mock_upload):
        """push handles upload error results without crashing."""
        mock_upload.return_value = iter([
            {"resourceType": "Patient", "source_id": "test-1",
             "server_id": None, "success": False,
             "error": "FHIR server error (ValueError)"},
        ])

        with tempfile.TemporaryDirectory() as d:
            input_path = Path(d) / "input.json"
            input_path.write_text(json.dumps(_SIMPLE_PATIENT), encoding="utf-8")

            # Should not raise even when upload reports errors
            main([
                "push",
                "--server", "http://target:8080/fhir",
                "--input", str(input_path),
            ])

            mock_upload.assert_called_once()

    @patch.dict(os.environ, {"FHIR_TARGET_URL": ""}, clear=False)
    def test_push_no_server_exits(self):
        """push without --server and empty FHIR_TARGET_URL -> SystemExit."""
        with tempfile.TemporaryDirectory() as d:
            input_path = Path(d) / "input.json"
            input_path.write_text(json.dumps(_SIMPLE_PATIENT), encoding="utf-8")

            with self.assertRaises(SystemExit) as ctx:
                main([
                    "push",
                    "--input", str(input_path),
                ])
            self.assertIn("--server", str(ctx.exception))

    def test_push_missing_input_exits(self):
        """push without required --input -> SystemExit(2) from argparse."""
        with self.assertRaises(SystemExit) as ctx:
            main(["push", "--server", "http://target:8080/fhir"])
        self.assertEqual(ctx.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
