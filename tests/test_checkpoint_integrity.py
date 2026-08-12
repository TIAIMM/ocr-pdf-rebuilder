from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from support import load_pipeline
from ocr_pdf_rebuilder.code_identity import package_source_identity


class CheckpointIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.pipeline = load_pipeline(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_checkpoint_round_trip_and_tamper_rejection(self):
        path = self.root / "state.json"
        payload = {"schema": 2, "status": "complete", "pages": [1, 2, 3]}
        self.pipeline.write_checkpoint(path, payload)
        self.assertEqual(self.pipeline.read_checkpoint(path), payload)

        record = json.loads(path.read_text(encoding="utf-8"))
        record["status"] = "split"
        path.write_text(json.dumps(record), encoding="utf-8")
        self.assertIsNone(self.pipeline.read_checkpoint(path))

    def test_reserved_checksum_field_is_rejected(self):
        with self.assertRaises(ValueError):
            self.pipeline.write_checkpoint(
                self.root / "bad.json", {"_checkpoint_sha256": "forged"}
            )

    def test_package_source_identity_is_path_independent_and_content_sensitive(self):
        first = self.root / "first/ocr_pdf_rebuilder"
        second = self.root / "second/ocr_pdf_rebuilder"
        first.mkdir(parents=True)
        second.mkdir(parents=True)
        for package in (first, second):
            (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
            (package / "worker.py").write_text("def run(): return 1\n", encoding="utf-8")
        self.assertEqual(package_source_identity(first), package_source_identity(second))

        (second / "worker.py").write_text("def run(): return 2\n", encoding="utf-8")
        self.assertNotEqual(
            package_source_identity(first)["files_sha256"],
            package_source_identity(second)["files_sha256"],
        )

    def test_mineru_checkpoint_identity_binds_current_implementation(self):
        source = self.root / "source.pdf"
        source.write_bytes(b"source")
        with (
            mock.patch.object(
                self.pipeline,
                "source_file_signature",
                return_value={"path": "source.pdf", "size": 6, "sha256": "a" * 64},
            ),
            mock.patch.object(
                self.pipeline,
                "mineru_parser_config_hash",
                return_value="b" * 64,
            ),
            mock.patch.object(
                self.pipeline,
                "current_package_source_identity_hash",
                return_value="c" * 64,
            ),
            mock.patch.object(
                self.pipeline,
                "mineru_runtime_identity_hash",
                return_value="d" * 64,
            ),
        ):
            identity = self.pipeline.checkpoint_identity(source, 0, 9)
        self.assertEqual(identity["implementation_identity_hash"], "c" * 64)

    def test_mineru_completion_without_current_implementation_is_rejected(self):
        state_path = self.root / "output.version"
        self.pipeline.write_checkpoint(
            state_path,
            {
                "schema": self.pipeline.MINERU_CHECKPOINT_SCHEMA,
                "output_version": self.pipeline.OUTPUT_VERSION,
                "implementation_identity_hash": "a" * 64,
            },
        )
        with mock.patch.object(
            self.pipeline,
            "current_package_source_identity_hash",
            return_value="b" * 64,
        ):
            self.assertFalse(
                self.pipeline.completed_output_state_matches(
                    state_path,
                    self.root / "source.pdf",
                    self.root / "output.pdf",
                    self.root / "output.md",
                    self.root / "output-images.pdf",
                )
            )

    def test_timestamp_only_runtime_changes_do_not_invalidate_completion(self):
        current = {
            "identity_schema": 2,
            "mineru_command": "mineru",
            "mineru_version_output": "3.3.1",
            "mineru_command_error": None,
            "mineru_executable": {
                "path": "/bin/mineru",
                "size": 10,
                "sha256": "a" * 64,
            },
            "python": {
                "executable": "/bin/python",
                "version": "3.12.13",
                "platform": "linux",
            },
            "packages": {"mineru": "3.3.1"},
            "models": [
                {
                    "path": "/models/current",
                    "files": [
                        {"path": "model.safetensors", "size": 20, "sha256": "b" * 64}
                    ],
                }
            ],
        }
        current = self.pipeline.normalize_runtime_identity(current)
        persisted = copy.deepcopy(current)
        persisted["mineru_executable"]["mtime_ns"] = 111
        persisted["models"][0]["files"][0]["mtime_ns"] = 222
        state = {
            "runtime_identity": persisted,
            "runtime_identity_hash": self.pipeline.stable_json_hash(persisted),
        }
        expected = self.pipeline.stable_json_hash(current)
        with mock.patch.object(
            self.pipeline, "mineru_runtime_identity_hash", return_value=expected
        ):
            self.assertTrue(self.pipeline.completed_runtime_identity_matches(state))

        state["runtime_identity"]["packages"]["mineru"] = "tampered"
        with mock.patch.object(
            self.pipeline, "mineru_runtime_identity_hash", return_value=expected
        ):
            self.assertFalse(self.pipeline.completed_runtime_identity_matches(state))


if __name__ == "__main__":
    unittest.main()
