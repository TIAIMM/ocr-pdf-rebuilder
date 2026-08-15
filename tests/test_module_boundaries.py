from __future__ import annotations

import ast
from pathlib import Path
import re
import tempfile
import threading
import unittest

import fitz

from ocr_pdf_rebuilder.forward_page_leak import (
    ForwardPageLeakAnalyzer,
    ForwardPageLeakConfig,
)
from ocr_pdf_rebuilder.pdf_validation import PdfArtifactValidator
from ocr_pdf_rebuilder.process_control import LiveProcessController
from ocr_pdf_rebuilder.component_runtime import ComponentRuntime
from ocr_pdf_rebuilder import (
    layout_engine,
    mineru_results,
    mineru_api_session,
    mineru_runner,
    page_recovery,
    pipeline_orchestrator,
    qc_reporting,
    reportlab_renderer,
    runtime_state,
    text_processing,
)
from support import load_pipeline


class PipelineModuleBoundaryTests(unittest.TestCase):
    def test_focused_modules_do_not_import_cli_entry_module(self):
        package_dir = Path(__file__).resolve().parents[1] / "src/ocr_pdf_rebuilder"
        for name in (
            "batch_runner.py",
            "component_runtime.py",
            "forward_page_leak.py",
            "layout_engine.py",
            "mineru_results.py",
            "mineru_api_session.py",
            "mineru_runner.py",
            "page_recovery.py",
            "pdf_validation.py",
            "pipeline_config.py",
            "pipeline_orchestrator.py",
            "process_control.py",
            "qc_reporting.py",
            "reportlab_renderer.py",
            "runtime_state.py",
            "text_processing.py",
        ):
            source = (package_dir / name).read_text(encoding="utf-8")
            imported_modules = []
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, ast.Import):
                    imported_modules.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported_modules.append(node.module)
            forbidden_entries = ("mineru_pipeline", "paddle_textonly_pdf")
            self.assertFalse(
                any(
                    entry in module
                    for module in imported_modules
                    for entry in forbidden_entries
                ),
                name,
            )

    def test_extracted_components_expose_standalone_exports(self):
        modules = (
            text_processing,
            mineru_results,
            mineru_runner,
            page_recovery,
            layout_engine,
            reportlab_renderer,
            qc_reporting,
            runtime_state,
            pipeline_orchestrator,
        )
        for module in modules:
            exports = module.component_exports()
            self.assertTrue(exports, module.__name__)
            self.assertTrue(any(callable(value) for value in exports.values()))

    def test_component_state_round_trips_through_compatibility_namespace(self):
        module_globals = {"STATE": 0}

        def increment():
            module_globals["STATE"] += 1
            return module_globals["STATE"]

        module_globals["increment"] = increment
        runtime = ComponentRuntime(module_globals, ("STATE", "increment"))
        namespace = {"STATE": 4, "increment": increment}
        self.assertEqual(runtime.invoke("increment", namespace), 5)
        self.assertEqual(namespace["STATE"], 5)

    def test_direct_entry_load_refreshes_runtime_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = load_pipeline(root / "first")
            second = load_pipeline(root / "second")
            self.assertNotEqual(first.RUNTIME_ROOT, second.RUNTIME_ROOT)

    def test_forward_page_analyzer_is_usable_without_pipeline_import(self):
        analyzer = ForwardPageLeakAnalyzer(
            ForwardPageLeakConfig(retry_reason="test"),
            normalize_text=lambda value: str(value or "").strip(),
            source_text_from_cell=lambda cell: str(cell.get("text", "")),
            retry_result_has_usable_cells=lambda result: bool(result.get("cells")),
            append_retry_reason=lambda result, reason: result.update(retry_reason=reason),
        )
        self.assertEqual(analyzer.normalize_overlap_text("Ａ b-1"), "ab1")
        self.assertEqual(
            analyzer.page_result_primary_text({"cells": [{"text": "page local"}]}),
            "page local",
        )

    def test_pdf_validator_scans_artifact_without_pipeline_import(self):
        with tempfile.TemporaryDirectory() as temporary:
            pdf_path = Path(temporary) / "artifact.pdf"
            document = fitz.open()
            document.new_page().insert_text((36, 36), "page one")
            document.new_page()
            document.save(pdf_path)
            document.close()

            validator = PdfArtifactValidator(
                radical_pattern=re.compile(r"[\u2e80-\u2fff]"),
                control_pattern=re.compile(r"[\x00-\x08]"),
                latex_pattern=re.compile(r"\\frac\b"),
            )
            scan = validator.scan(pdf_path, include_markdown=False, checks=set())
            self.assertEqual(scan["page_count"], 2)
            validator.validate_page_count(pdf_path, 2, scan)

    def test_process_controller_exposes_process_group_primitives(self):
        controller = LiveProcessController(
            logger=lambda _message: None,
            console_lock=threading.Lock(),
            exit_cleanup_seconds=0.1,
        )
        self.assertFalse(controller.posix_process_group_exists(None))

    def test_extracted_runtime_modules_own_required_module_dependencies(self):
        self.assertEqual(mineru_runner.math.ceil(61 / 60), 2)
        self.assertIs(mineru_runner.sys, __import__("sys"))
        self.assertTrue(callable(runtime_state.time.time))
        self.assertTrue(callable(runtime_state.threading.get_ident))
        self.assertTrue(callable(runtime_state.fitz.open))


if __name__ == "__main__":
    unittest.main()
