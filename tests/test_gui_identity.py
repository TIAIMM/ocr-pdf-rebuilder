"""Identity and artifact provenance tests: temporary controllers and fake workers only."""
import json
import hashlib
from contextlib import contextmanager
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys
import threading
from unittest.mock import Mock, patch

import pytest


@pytest.mark.parametrize("names", [[], ["../outside.pdf"], ["sub/file.pdf"], ["/tmp/file.pdf"],
                                   ["file.txt"], [None], ["a.pdf", "a.pdf"]])
def test_directory_selection_rejects_invalid_names(controller, names):
    controller.input_dir.mkdir()
    (controller.input_dir / "a.pdf").write_bytes(b"fixture")
    with patch.object(controller, "start") as start:
        with pytest.raises(ValueError):
            controller.start_directory("paddle", names)
        start.assert_not_called()


def test_directory_selection_uses_stable_default_and_unique_output(controller):
    controller.input_dir.mkdir()
    source = controller.input_dir / "a.pdf"
    source.write_bytes(b"fixture")
    baseline = controller.output_dir
    with patch.object(controller, "start") as start:
        controller.start_directory("paddle", ["a.pdf"])
        first = start.call_args.kwargs
        assert first["input_files"] == [str(source.resolve())]
        assert first["run_id"].startswith("standalone-")
        assert Path(first["output_dir"]) == baseline
        controller.output_dir = Path(first["output_dir"])
        controller.start_directory("paddle", ["a.pdf"])
        second = start.call_args.kwargs
        assert Path(second["output_dir"]) == baseline
        assert second["run_id"] != first["run_id"]
        assert controller.status()["default_output_dir"] == str(baseline)
        override = str(baseline.parent / "manual-override")
        controller.start_directory("paddle", ["a.pdf"], output_dir=override)
        assert start.call_args.kwargs["output_dir"] == override


def test_standalone_http_directory_checkbox_selection_and_legacy(controller):
    controller.input_dir.mkdir()
    (controller.input_dir / "a.pdf").write_bytes(b"fixture")
    (controller.input_dir / "b.pdf").write_bytes(b"fixture")
    with serving(controller) as request, patch.object(controller, "start") as start:
        status, payload = request("GET", "/api/status", auth=False)
        assert status == 200 and len(payload["directory_inputs"]) == 2
        selected_names = [item["name"] for item in payload["directory_inputs"]]
        # A PDF arriving after the displayed select-all snapshot is not included.
        (controller.input_dir / "arrived-later.pdf").write_bytes(b"fixture")
        destinations = []
        for _ in range(2):
            status, _ = request("POST", "/api/start", {
                "pipeline": "paddle", "input_names": selected_names}, auth=False)
            assert status == 200
            args = start.call_args.kwargs
            assert args["input_files"] == [str((controller.input_dir / name).resolve()) for name in selected_names]
            assert Path(args["output_dir"]) == controller.output_dir
            destinations.append(args["output_dir"])
        assert destinations[0] == destinations[1]
        status, _ = request("POST", "/api/start", {"pipeline": "paddle", "input_names": ["b.pdf"]}, auth=False)
        assert status == 200
        assert start.call_args.kwargs["input_files"] == [str((controller.input_dir / "b.pdf").resolve())]
        status, _ = request("POST", "/api/start", {"pipeline": "paddle"}, auth=False)
        assert status == 200
        start.assert_called_with(pipeline="paddle")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ocr_pdf_rebuilder.gui import GuiController, make_handler, _file_stamp


@pytest.fixture
def controller(tmp_path):
    return GuiController(tmp_path / "input", tmp_path / "output", tmp_path / "summary.json",
                         command_factory=lambda: ["fixture-worker-only"])


@contextmanager
def serving(controller):
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(controller, "csrf", "secret"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    conn = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    def request(method, path, data=None, auth=True):
        headers = {"Authorization": "Bearer secret"} if auth else {"X-CSRF-Token": "csrf"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, json.dumps(data) if data is not None else None, headers)
        response = conn.getresponse()
        return response.status, json.loads(response.read())
    try:
        yield request
    finally:
        conn.close()
        server.shutdown()
        server.server_close()
        thread.join(3)


def worker(controller):
    process = Mock(pid=123)
    process.poll.return_value = None
    controller._process = process
    controller._run_id = 4
    controller._status = "running"
    return process


def test_identity_shared_by_all_snapshots_and_new_on_restart(controller):
    controller._append_log("fixture log\n")
    with serving(controller) as request:
        payloads = [request("GET", path)[1] for path in
                    ("/api/status", "/api/log", "/workbench/v1/status",
                     "/workbench/v1/log", "/workbench/v1/health")]
        assert len({p["instance_id"] for p in payloads}) == 1
        assert all(p["instance_id"] == p["service_instance_id"] for p in payloads)
        assert payloads[0]["capabilities"]["conditional_stop"] is True
        assert payloads[-1]["capabilities"]["conditional_shutdown"] is True
    fresh = GuiController(controller.input_dir, controller.output_dir, controller.summary_path)
    assert fresh.service_instance_id != controller.service_instance_id


@pytest.mark.parametrize("body", [
    {"expected_instance_id": "stale", "expected_run_id": 4},
    {"expected_run_id": 3},
    {"expected_run_id": True},
    {"expected_run_id": "4"},
    {"expected_run_id": None},
    {"expected_instance_id": None},
    {"expected_instance_id": ""},
    {"expected_run_id": -1},
    {"run_id": "another-workflow"},
    {"extra": 1},
    {"expected_instance_id": "one", "expected_service_instance_id": "two"},
])
def test_rejected_stop_never_signals(controller, body):
    process = worker(controller)
    with serving(controller) as request, patch("ocr_pdf_rebuilder.gui.os.killpg", create=True) as kill:
        status, _ = request("POST", "/workbench/v1/stop", body)
        assert status == 409
        assert controller._status == "running"
        kill.assert_not_called()
        process.send_signal.assert_not_called()
        process.terminate.assert_not_called()


def test_workflow_stop_all_three_conditions_and_legacy_supported(controller):
    process = worker(controller)
    controller._selected_run = {"run_id": "workflow-step", "input_files": [], "sources": []}
    with serving(controller) as request, patch("ocr_pdf_rebuilder.gui.os.killpg", create=True), \
            patch("ocr_pdf_rebuilder.gui.threading.Timer"):
        code, schema = request("GET", "/workbench/v1/operations")
        props = schema["operations"]["stop"]["schema"]["properties"]
        assert {"expected_instance_id", "expected_run_id", "run_id"} <= set(props)
        code, result = request("POST", "/workbench/v1/stop", {
            "expected_instance_id": controller.service_instance_id, "expected_run_id": 4,
            "run_id": "workflow-step"})
        assert code == 200
        assert result["instance_id"] == controller.service_instance_id
        assert result["run_id"] == 4 and result["external_run_id"] == "workflow-step"
        assert controller._status == "stopping"
        # Legacy callers still work; repeated stop requests are idempotent.
        assert request("POST", "/workbench/v1/stop", {})[0] == 200
        assert request("POST", "/workbench/v1/stop", {"run_id": "workflow-step"})[0] == 200


def test_condition_is_checked_after_acquiring_controller_lock(controller):
    process = worker(controller)
    entered = threading.Event()
    result = []
    def stop():
        entered.set()
        try:
            controller.stop(expected_instance_id=controller.service_instance_id, expected_run_id=4)
        except RuntimeError as error:
            result.append(str(error))
    with controller._lock:
        thread = threading.Thread(target=stop)
        thread.start()
        assert entered.wait(1)
        # Simulate another run winning the lock before this stale stop request.
        controller._run_id = 5
    thread.join(2)
    assert not thread.is_alive()
    assert result and "expected_run_id" in result[0]
    process.send_signal.assert_not_called()


def test_signal_and_identity_validation_share_lock(controller):
    process = worker(controller)
    owned = []
    def observe(*args):
        owned.append(controller._lock._is_owned())
    process.send_signal.side_effect = observe
    process.terminate.side_effect = observe
    with patch("ocr_pdf_rebuilder.gui.os.killpg", side_effect=observe, create=True), \
            patch("ocr_pdf_rebuilder.gui.threading.Timer"):
        controller.stop(expected_instance_id=controller.service_instance_id, expected_run_id=4)
    assert owned == [True]


def test_log_cursor_resets_even_when_new_log_is_longer(controller):
    controller._append_log("a new run with a much longer log\n")
    controller._run_id = 2
    for conditions in ({"expected_run_id": 1}, {"expected_instance_id": "old-instance"}):
        log = controller.log_since(5, **conditions)
        assert log["reset"] is True and log["text"].startswith("a new run")
        assert log["instance_id"] == controller.service_instance_id and log["run_id"] == 2
    assert controller.log_since(5, expected_run_id=2)["reset"] is False


def test_shutdown_rejects_active_or_stale_and_blocks_future_start(controller):
    worker(controller)
    with serving(controller) as request:
        body = {"expected_instance_id": controller.service_instance_id, "expected_run_id": 4}
        assert request("POST", "/workbench/v1/shutdown", body)[0] == 409
        controller._process = None
        assert request("POST", "/workbench/v1/shutdown", {**body, "expected_run_id": 3})[0] == 409
        assert request("POST", "/workbench/v1/shutdown", {})[0] == 409
        assert request("POST", "/workbench/v1/shutdown", body)[0] == 200
        with pytest.raises(RuntimeError, match="shutting down"):
            controller.start()


def test_standalone_stop_query_is_conditional(controller):
    worker(controller)
    with serving(controller) as request:
        code, _ = request("POST", "/api/stop?expected_instance_id=stale&expected_run_id=4", auth=False)
        assert code == 409 and controller._status == "running"


@pytest.mark.parametrize("selected", [True, False])
def test_outputs_require_explicit_report_path_and_current_run_binding(controller, selected):
    controller.output_dir.mkdir()
    controller.input_dir.mkdir()
    source = str(controller.input_dir / "book.pdf")
    Path(source).write_bytes(b"source fixture")
    proven = controller.output_dir / "unrelated-title.pdf"
    lookalike = controller.output_dir / "book_searchable.pdf"
    proven.write_bytes(b"fixture")
    lookalike.write_bytes(b"fixture")
    if selected:
        controller._selected_run = {"run_id": "workflow", "input_files": [source], "sources": []}
    controller._run_sources = {source: _file_stamp(Path(source))}
    controller._started_at = 100
    external_id = "workflow" if selected else None
    report = {"external_run_id": external_id, "started_at": 101, "output_dir": str(controller.output_dir),
              "results": [{"status": "completed", "input_pdf": source, "output_searchable_pdf": str(proven)}]}
    manifest = {"source": {"path": source, "size": Path(source).stat().st_size,
                           "sha256": hashlib.sha256(Path(source).read_bytes()).hexdigest()},
                "searchable_pdf": {"path": str(proven), "size": proven.stat().st_size,
                                   "sha256": hashlib.sha256(proven.read_bytes()).hexdigest()}}
    marker = controller.output_dir / "arbitrary-name.version"
    marker.write_text(json.dumps(manifest), encoding="utf-8")
    controller.summary_path.write_text(json.dumps(report), encoding="utf-8")
    outputs = {p["name"]: p for p in controller.list_outputs()}
    assert outputs[proven.name]["current_run"] is True
    assert outputs[proven.name]["provenance"]["status"] == "verified"
    assert outputs[proven.name]["provenance"]["source_path"] == source
    assert outputs[lookalike.name]["current_run"] is False
    assert outputs[lookalike.name]["provenance"]["status"] == "unverified"
    report["external_run_id"] = "older-workflow"
    controller.summary_path.write_text(json.dumps(report), encoding="utf-8")
    assert all(not p["current_run"] for p in controller.list_outputs())
    report["external_run_id"] = external_id
    report["started_at"] = 99
    controller.summary_path.write_text(json.dumps(report), encoding="utf-8")
    assert all(not p["current_run"] for p in controller.list_outputs())
    report["started_at"] = 101
    controller.summary_path.write_text(json.dumps(report), encoding="utf-8")
    proven.write_bytes(b"changed")
    assert all(not p["current_run"] for p in controller.list_outputs())
    proven.write_bytes(b"fixture")
    Path(source).write_bytes(b"changed source")
    assert all(not p["current_run"] for p in controller.list_outputs())


def test_report_without_hash_manifest_is_reported_but_not_proven_current(controller):
    controller.output_dir.mkdir()
    output = controller.output_dir / "book.pdf"
    output.write_bytes(b"fixture")
    controller._started_at = 100
    controller.summary_path.write_text(json.dumps({"started_at": 101, "results": [
        {"input_pdf": str(controller.input_dir / "source.pdf"), "output_text_pdf": str(output)}]}))
    item = controller.list_outputs()[0]
    assert item["current_run"] is False and item["provenance"]["status"] == "reported"


def test_history_persists_without_restoring_service_identity(controller):
    controller._history = [{"run_id": 4, "status": "completed"}]
    controller._persist_run_state()
    restored = GuiController(controller.input_dir, controller.output_dir, controller.summary_path)
    assert restored.status()["history"] == controller._history
    assert restored.service_instance_id != controller.service_instance_id

def test_default_mode_restores_run_binding_and_numeric_id(controller):
    controller._run_id = 8
    controller._started_at = 100
    controller._status = "completed"
    controller._run_sources = {"source-path": {"size": 10}}
    controller._persist_run_state()
    restored = GuiController(controller.input_dir, controller.output_dir, controller.summary_path)
    assert restored._selected_run is None and restored.status()["run_id"] == 8
    assert restored._started_at == 100 and restored._run_sources == controller._run_sources
    assert restored.service_instance_id != controller.service_instance_id


def test_markdown_provenance_uses_manifest_paths_not_prefixes(controller):
    controller.input_dir.mkdir()
    controller.output_dir.mkdir()
    source = controller.input_dir / "source.pdf"
    source.write_bytes(b"source")
    pdf = controller.output_dir / "report.pdf"
    pdf.write_bytes(b"pdf")
    markdown = controller.output_dir / "totally-different-title.md"
    markdown.write_bytes(b"markdown")
    def signature(path):
        return {"path": str(path), "size": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    controller._run_sources = {str(source): _file_stamp(source)}
    controller._started_at = 100
    report = {"started_at": 101, "output_dir": str(controller.output_dir), "results": [
        {"status": "skipped", "input_pdf": str(source), "output_text_pdf": str(pdf)}]}
    controller.summary_path.write_text(json.dumps(report), encoding="utf-8")
    manifest = {"source": signature(source), "text_pdf": signature(pdf), "markdown": signature(markdown)}
    marker = controller.output_dir / "unrelated.version"
    marker.write_text(json.dumps(manifest), encoding="utf-8")
    output = next(p for p in controller.list_outputs() if p["name"] == markdown.name)
    assert output["current_run"] and output["provenance"]["field"] == "markdown"
    manifest["_checkpoint_sha256"] = "invalid checksum"
    marker.write_text(json.dumps(manifest), encoding="utf-8")
    assert all(not p["current_run"] for p in controller.list_outputs())
