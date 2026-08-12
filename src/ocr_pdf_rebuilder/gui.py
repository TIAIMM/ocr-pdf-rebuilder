from __future__ import annotations

import argparse
import codecs
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import quote, unquote, urlparse
import webbrowser


DEFAULT_INPUT_DIR = Path.home() / "ocr_jobs/input"
DEFAULT_OUTPUT_DIR = Path.home() / "ocr_jobs/pdf_mineru"
DEFAULT_SUMMARY_PATH = Path.home() / "ocr_jobs/logs_mineru/batch_summary.json"
MAX_LOG_CHARS = 400_000


def file_record(path: Path) -> dict[str, object]:
    stat = path.stat()
    return {
        "name": path.name,
        "size": stat.st_size,
        "modified_at": stat.st_mtime,
    }


class GuiController:
    def __init__(
        self,
        input_dir: Path = DEFAULT_INPUT_DIR,
        output_dir: Path = DEFAULT_OUTPUT_DIR,
        summary_path: Path = DEFAULT_SUMMARY_PATH,
        command_factory=None,
    ):
        self.input_dir = Path(input_dir)
        self.output_dir = Path(output_dir)
        self.summary_path = Path(summary_path)
        self.command_factory = command_factory or self._default_command
        self._lock = threading.RLock()
        self._process: subprocess.Popen[bytes] | None = None
        self._status = "idle"
        self._returncode: int | None = None
        self._started_at: float | None = None
        self._finished_at: float | None = None
        self._run_id = 0
        self._log_chunks: deque[str] = deque()
        self._log_chars = 0
        self._log_start = 0

    @staticmethod
    def _default_command() -> list[str]:
        return [sys.executable, "-u", "-m", "ocr_pdf_rebuilder.mineru_textonly_pdf"]

    @staticmethod
    def _source_environment() -> dict[str, str]:
        env = os.environ.copy()
        source_root = str(Path(__file__).resolve().parents[1])
        current = env.get("PYTHONPATH")
        env["PYTHONPATH"] = source_root if not current else os.pathsep.join((source_root, current))
        env["PYTHONUNBUFFERED"] = "1"
        return env

    def _append_log(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            self._log_chunks.append(text)
            self._log_chars += len(text)
            while self._log_chars > MAX_LOG_CHARS and self._log_chunks:
                removed = self._log_chunks.popleft()
                self._log_chars -= len(removed)
                self._log_start += len(removed)

    def _clear_log(self) -> None:
        self._log_chunks.clear()
        self._log_chars = 0
        self._log_start = 0

    def list_inputs(self) -> list[dict[str, object]]:
        if not self.input_dir.exists():
            return []
        return [file_record(path) for path in sorted(self.input_dir.glob("*.pdf")) if path.is_file()]

    def list_outputs(self) -> list[dict[str, object]]:
        if not self.output_dir.exists():
            return []
        records = []
        for path in sorted(self.output_dir.glob("*.pdf"), key=lambda item: item.stat().st_mtime, reverse=True):
            if path.is_file():
                record = file_record(path)
                record["download_url"] = "/files/output/" + quote(path.name)
                records.append(record)
        return records

    def read_summary(self) -> dict[str, object] | None:
        try:
            payload = json.loads(self.summary_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        payload.pop("_checkpoint_sha256", None)
        return payload

    def start(self) -> None:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                raise RuntimeError("生成任务已经在运行")
            inputs = self.list_inputs()
            if not inputs:
                raise RuntimeError(f"输入目录中没有 PDF：{self.input_dir}")

            self.input_dir.mkdir(parents=True, exist_ok=True)
            self.output_dir.mkdir(parents=True, exist_ok=True)
            self._clear_log()
            self._run_id += 1
            self._status = "starting"
            self._returncode = None
            self._started_at = time.time()
            self._finished_at = None

            creationflags = 0
            if os.name == "nt":
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
            process = subprocess.Popen(
                self.command_factory(),
                cwd=str(Path(__file__).resolve().parents[2]),
                env=self._source_environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
                creationflags=creationflags,
            )
            self._process = process
            self._status = "running"
            self._append_log(
                f"[GUI] 已启动生成任务，PID={process.pid}，输入 PDF={len(inputs)}\n"
            )
            threading.Thread(target=self._monitor, args=(process,), daemon=True).start()

    def _monitor(self, process: subprocess.Popen[bytes]) -> None:
        assert process.stdout is not None
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while True:
                chunk = process.stdout.read(65536)
                if not chunk:
                    break
                self._append_log(decoder.decode(chunk))
            self._append_log(decoder.decode(b"", final=True))
            returncode = process.wait()
        except Exception as exc:
            self._append_log(f"\n[GUI] 读取任务输出失败：{type(exc).__name__}: {exc}\n")
            returncode = process.poll()
            if returncode is None:
                returncode = -1
        finally:
            process.stdout.close()

        with self._lock:
            if self._process is process:
                self._returncode = returncode
                self._finished_at = time.time()
                self._status = "completed" if returncode == 0 else "failed"
                self._process = None
        label = "正常完成" if returncode == 0 else f"结束，退出码={returncode}"
        self._append_log(f"\n[GUI] 任务{label}\n")

    def stop(self) -> None:
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None:
                raise RuntimeError("当前没有正在运行的任务")
            if self._status == "stopping":
                return
            self._status = "stopping"
            self._append_log(
                "\n[GUI] 正在请求安全停止；MinerU 子进程清理最多可能需要约 20 秒……\n"
            )
            if os.name == "posix":
                process.send_signal(signal.SIGINT)
            elif hasattr(signal, "CTRL_BREAK_EVENT"):
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                process.terminate()

    def status(self) -> dict[str, object]:
        with self._lock:
            process = self._process
            return {
                "status": self._status,
                "running": process is not None and process.poll() is None,
                "pid": process.pid if process is not None and process.poll() is None else None,
                "returncode": self._returncode,
                "started_at": self._started_at,
                "finished_at": self._finished_at,
                "run_id": self._run_id,
                "input_dir": str(self.input_dir),
                "output_dir": str(self.output_dir),
                "inputs": self.list_inputs(),
                "outputs": self.list_outputs(),
                "summary": self.read_summary(),
            }

    def log_since(self, offset: int) -> dict[str, object]:
        with self._lock:
            current_end = self._log_start + self._log_chars
            reset = offset < self._log_start or offset > current_end
            if reset:
                offset = self._log_start
            skip = offset - self._log_start
            text = "".join(self._log_chunks)
            return {
                "text": text[skip:],
                "next_offset": current_end,
                "reset": reset,
                "run_id": self._run_id,
            }

    def output_file(self, encoded_name: str) -> Path | None:
        name = unquote(encoded_name)
        if not name or Path(name).name != name or not name.lower().endswith(".pdf"):
            return None
        candidate = self.output_dir / name
        try:
            if not candidate.is_file() or candidate.resolve().parent != self.output_dir.resolve():
                return None
        except OSError:
            return None
        return candidate


HTML_PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>OCR PDF 生成器</title>
  <style>
    :root { color-scheme: light; --ink:#17212b; --muted:#607080; --line:#dce3e8; --brand:#176b5b; --brand2:#e8f4f1; --danger:#b63838; }
    * { box-sizing:border-box; }
    body { margin:0; background:#f4f7f6; color:var(--ink); font-family:system-ui,"Microsoft YaHei",sans-serif; }
    main { width:min(1100px,calc(100% - 32px)); margin:28px auto 48px; }
    header { display:flex; align-items:flex-end; justify-content:space-between; gap:16px; margin-bottom:18px; }
    h1 { margin:0; font-size:25px; font-weight:720; letter-spacing:.02em; }
    .subtitle,.path,.hint { color:var(--muted); font-size:13px; }
    .status { padding:7px 12px; border-radius:999px; background:#e8ecef; font-size:13px; font-weight:650; white-space:nowrap; }
    .status.running,.status.starting { color:#075d4c; background:#d9f2eb; }
    .status.stopping { color:#875800; background:#fff0c9; }
    .status.failed { color:#9b2727; background:#fde2e2; }
    .grid { display:grid; grid-template-columns:1fr 1fr; gap:16px; }
    .card { background:white; border:1px solid var(--line); border-radius:12px; padding:17px; box-shadow:0 2px 10px rgba(24,45,42,.035); }
    .wide { grid-column:1/-1; }
    .card h2 { margin:0 0 12px; font-size:16px; }
    .actions { display:flex; gap:10px; flex-wrap:wrap; margin:15px 0 8px; }
    button { border:0; border-radius:8px; padding:10px 17px; font:inherit; font-weight:650; cursor:pointer; background:var(--brand); color:white; }
    button.secondary { background:white; color:var(--danger); border:1px solid #e1baba; }
    button:disabled { opacity:.45; cursor:not-allowed; }
    ul { list-style:none; padding:0; margin:0; max-height:240px; overflow:auto; }
    li { display:flex; justify-content:space-between; gap:12px; padding:8px 0; border-top:1px solid #edf0f2; font-size:13px; }
    li:first-child { border-top:0; }
    a { color:var(--brand); text-decoration:none; font-weight:600; }
    a:hover { text-decoration:underline; }
    .empty { color:var(--muted); padding:16px 0; font-size:13px; }
    .summary { display:grid; grid-template-columns:repeat(4,1fr); gap:8px; }
    .metric { background:#f5f8f7; padding:10px; border-radius:8px; }
    .metric b { display:block; font-size:20px; margin-top:2px; }
    pre { min-height:260px; max-height:460px; overflow:auto; margin:0; padding:14px; border-radius:9px; background:#101817; color:#dcebe7; font:12px/1.55 Consolas,monospace; white-space:pre-wrap; word-break:break-word; }
    .message { min-height:20px; margin-top:8px; color:var(--danger); font-size:13px; }
    @media (max-width:760px) { .grid { grid-template-columns:1fr; } .wide { grid-column:auto; } header { align-items:flex-start; flex-direction:column; } .summary { grid-template-columns:1fr 1fr; } }
  </style>
</head>
<body>
<main>
  <header><div><h1>OCR PDF 生成器</h1><div class="subtitle">MinerU 纯文本 PDF 批处理控制面板</div></div><div id="status" class="status">读取中</div></header>
  <section class="grid">
    <div class="card">
      <h2>控制</h2>
      <div id="inputPath" class="path"></div>
      <div class="actions"><button id="start">开始生成</button><button id="stop" class="secondary">安全停止</button></div>
      <div class="hint">每次处理输入目录中的全部 PDF；已完成且完整性匹配的文件会复用检查点。</div>
      <div id="message" class="message"></div>
    </div>
    <div class="card">
      <h2>批次摘要</h2>
      <div id="summary" class="summary"></div>
    </div>
    <div class="card">
      <h2>输入文件</h2>
      <ul id="inputs"></ul>
    </div>
    <div class="card">
      <h2>生成结果</h2>
      <ul id="outputs"></ul>
    </div>
    <div class="card wide">
      <h2>实时日志</h2>
      <pre id="log">等待任务启动……</pre>
    </div>
  </section>
</main>
<script>
const csrf = __CSRF_TOKEN__;
let logOffset = 0, runId = null;
const $ = id => document.getElementById(id);
const fmtSize = n => n < 1048576 ? `${(n/1024).toFixed(1)} KB` : `${(n/1048576).toFixed(1)} MB`;
const labels = {idle:'空闲',starting:'正在启动',running:'正在生成',stopping:'正在安全停止',completed:'已完成',failed:'已结束（有错误）'};
function renderFiles(id, files, downloadable=false) {
  const target=$(id); target.textContent='';
  if (!files.length) { const d=document.createElement('div'); d.className='empty'; d.textContent='暂无 PDF'; target.append(d); return; }
  files.forEach(file => { const li=document.createElement('li'); const name=document.createElement(downloadable?'a':'span'); name.textContent=file.name; if(downloadable) name.href=file.download_url; const size=document.createElement('span'); size.className='path'; size.textContent=fmtSize(file.size); li.append(name,size); target.append(li); });
}
function renderSummary(summary) {
  const counts=(summary&&summary.counts)||{}; const values=[['状态',summary?summary.status:'暂无'],['已完成',counts.completed||0],['已跳过',counts.skipped||0],['失败',counts.failed||0]];
  $('summary').textContent=''; values.forEach(([k,v])=>{const d=document.createElement('div');d.className='metric';const s=document.createElement('span');s.className='path';s.textContent=k;const b=document.createElement('b');b.textContent=v;d.append(s,b);$('summary').append(d);});
}
async function refresh() {
  try { const r=await fetch('/api/status',{cache:'no-store'}); const s=await r.json();
    $('status').textContent=labels[s.status]||s.status; $('status').className='status '+s.status; $('start').disabled=s.running||!s.inputs.length; $('stop').disabled=!s.running||s.status==='stopping'; $('inputPath').textContent='输入：'+s.input_dir; renderFiles('inputs',s.inputs); renderFiles('outputs',s.outputs,true); renderSummary(s.summary);
    if(runId!==s.run_id){runId=s.run_id;logOffset=0;$('log').textContent='';}
    const lr=await fetch('/api/log?offset='+logOffset,{cache:'no-store'}); const l=await lr.json(); if(l.reset)$('log').textContent=''; if(l.text){$('log').textContent+=l.text;$('log').scrollTop=$('log').scrollHeight;} logOffset=l.next_offset;
  } catch(e) { $('message').textContent='无法连接 GUI 服务：'+e; }
}
async function action(path) { $('message').textContent=''; try { const r=await fetch(path,{method:'POST',headers:{'X-CSRF-Token':csrf}}); const data=await r.json(); if(!r.ok) throw new Error(data.error||r.statusText); await refresh(); } catch(e){$('message').textContent=e.message;} }
$('start').onclick=()=>action('/api/start'); $('stop').onclick=()=>action('/api/stop');
refresh(); setInterval(refresh,1500);
</script>
</body></html>
"""


def make_handler(controller: GuiController, csrf_token: str):
    html_page = HTML_PAGE.replace("__CSRF_TOKEN__", json.dumps(csrf_token))

    class Handler(BaseHTTPRequestHandler):
        server_version = "OcrPdfGui/1.0"

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: object) -> None:
            self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self._send(HTTPStatus.OK, html_page.encode("utf-8"), "text/html; charset=utf-8")
                return
            if parsed.path == "/api/status":
                self._json(HTTPStatus.OK, controller.status())
                return
            if parsed.path == "/api/log":
                try:
                    query = parsed.query.split("offset=", 1)[1].split("&", 1)[0]
                    offset = max(0, int(query))
                except (IndexError, ValueError):
                    offset = 0
                self._json(HTTPStatus.OK, controller.log_since(offset))
                return
            prefix = "/files/output/"
            if parsed.path.startswith(prefix):
                path = controller.output_file(parsed.path[len(prefix):])
                if path is None:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "文件不存在"})
                    return
                content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(path.stat().st_size))
                self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(path.name)}")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                with path.open("rb") as handle:
                    while chunk := handle.read(1024 * 1024):
                        self.wfile.write(chunk)
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "未找到"})

        def do_POST(self) -> None:
            if not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), csrf_token):
                self._json(HTTPStatus.FORBIDDEN, {"error": "请求令牌无效"})
                return
            try:
                if self.path == "/api/start":
                    controller.start()
                elif self.path == "/api/stop":
                    controller.stop()
                else:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "未找到"})
                    return
            except RuntimeError as exc:
                self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
                return
            except OSError as exc:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"操作失败：{exc}"})
                return
            self._json(HTTPStatus.OK, {"ok": True})

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OCR PDF Rebuilder 本地网页 GUI")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认：127.0.0.1）")
    parser.add_argument("--port", type=int, default=8765, help="监听端口（默认：8765）")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    controller = GuiController()
    csrf_token = secrets.token_urlsafe(24)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(controller, csrf_token))
    host_for_url = "localhost" if args.host in {"0.0.0.0", "127.0.0.1"} else args.host
    url = f"http://{host_for_url}:{server.server_port}/"
    print(f"OCR PDF GUI 已启动：{url}", flush=True)
    print("按 Ctrl+C 关闭 GUI；正在运行的生成任务需先在页面中安全停止。", flush=True)
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nGUI 已关闭。", flush=True)
    finally:
        if controller.status()["running"]:
            print("正在安全停止生成任务……", flush=True)
            try:
                controller.stop()
            except (RuntimeError, OSError):
                pass
            deadline = time.monotonic() + 30
            while controller.status()["running"] and time.monotonic() < deadline:
                time.sleep(0.1)
        server.server_close()


if __name__ == "__main__":
    main()
