"""Routed standalone UI fixtures; never contacts or starts a real OCR service.

Run with Playwright and installed Edge: python gui_browser_smoke.py
Screenshots are written into a fresh temporary directory.
"""
import json
from pathlib import Path
import sys
import tempfile
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ocr_pdf_rebuilder.gui_static import HTML_PAGE


def main():
    artifacts = Path(tempfile.mkdtemp(prefix="ocr-gui-viewport-"))
    state = {
        "instance_id": "fixture-instance", "service_instance_id": "fixture-instance",
        "capabilities": {"conditional_stop": True}, "run_id": 7, "external_run_id": None,
        "status": "running", "running": True, "pipeline": "paddle",
        "pipelines": [{"id": "paddle", "label": "PaddleOCR-VL"}, {"id": "mineru", "label": "MinerU"}],
        "input_dir": "/fixture/input", "output_dir": "/fixture/output/runs/previous",
        "default_output_dir": "/fixture/output", "default_output_dirs": {"paddle": "/fixture/output", "mineru": "/fixture/mineru"},
        "default_input_count": 24,
        "inputs": [{"name": f"原文与研究笔记 {i}.pdf", "size": 1800000} for i in range(24)],
        "outputs": [{"name": f"成果 {i}.pdf", "size": 123456, "current_run": False, "path": f"/fixture/output/{i}.pdf",
                     "download_url": f"/files/output/{i}.pdf", "provenance": {"status": "unverified"}} for i in range(30)],
        "file_progress": [{"name": f"文献 {i}.pdf", "stage": "逐页识别", "percent": 34,
                           "page_current": 650, "page_total": 1909, "status": "running"} for i in range(24)],
        "summary": {"counts": {"completed": 0}}, "history": [
            {"run_id": i, "status": "completed", "pipeline": "paddle",
             "started_at": 1700000000, "output_dir": "/fixture/历史输出"} for i in range(30)],
    }
    state["directory_inputs"] = state["inputs"]
    requests, errors, polls = [], [], []
    mismatch, offline, action_fail = [False], [False], [False]
    log = ["\n".join(f"[OCR fixture] 可读日志第 {n} 行 — 650 / 1909 页" for n in range(1, 41)) + "\n"]
    def route(route):
        request = route.request
        parsed = urlsplit(request.url)
        if parsed.path == "/":
            route.fulfill(body=HTML_PAGE.replace("__CSRF_TOKEN__", json.dumps("fixture-csrf")),
                          content_type="text/html; charset=utf-8")
        elif parsed.path == "/api/status":
            polls.append('status')
            route.fulfill(status=503 if offline[0] else 200, json=state)
        elif parsed.path == "/api/log":
            polls.append('log')
            offset = int(parse_qs(parsed.query).get("offset", ["0"])[0])
            instance = "replacement-fixture" if mismatch[0] else state["instance_id"]
            route.fulfill(json={"instance_id": instance, "service_instance_id": instance,
                                "run_id": state["run_id"], "text": log[0][offset:],
                                "next_offset": len(log[0]), "reset": False})
        elif request.method == "POST" and parsed.path in ("/api/start", "/api/stop"):
            requests.append((parsed, request.post_data, request.headers))
            route.fulfill(status=409 if action_fail[0] else 200,
                          json={"error": "fixture conflict"} if action_fail[0] else {"ok": True})
        else:
            route.abort()
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge")
        page = browser.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/*", route)
        def refresh():
            page.evaluate("async () => { await refresh(); }")
        def load(width, height):
            page.set_viewport_size({"width": width, "height": height})
            page.goto("http://127.0.0.1:54321/")
            page.wait_for_function("!polling && snapshot !== null")
            page.evaluate("clearTimeout(refreshTimer)")
        for width, height in [(1024, 600), (1280, 720), (1440, 900), (768, 600), (390, 844)]:
            load(width, height)
            sizes = page.evaluate("""() => {
                const p=$('log'),s=getComputedStyle(p),rect=p.getBoundingClientRect();
                return {outerX:document.documentElement.scrollWidth>innerWidth,
                        outerY:document.documentElement.scrollHeight>innerHeight,
                        lines:Math.floor((p.clientHeight-parseFloat(s.paddingTop)-parseFloat(s.paddingBottom))/parseFloat(s.lineHeight)),
                        logBottom:rect.bottom,height:innerHeight};
            }""")
            assert not sizes["outerX"], (width, sizes)
            if width >= 1024:
                assert not sizes["outerY"] and sizes["lines"] >= 8, (width, sizes)
                assert sizes["logBottom"] <= height and page.locator("#start").is_visible()
                assert page.locator("#files #start").count() == 1
            assert page.locator(".progress-card #stop").is_visible()
            assert page.evaluate("$('fileProgress').firstElementChild.getBoundingClientRect().bottom <= $('fileProgress').getBoundingClientRect().bottom")
            assert page.locator("#pauseLog").is_visible()
            assert page.locator("#copyLog").is_visible()
            assert page.locator("#latestLog").is_visible()
            assert '整体处理' in page.locator('#totalText').inner_text()
            page.screenshot(path=str(artifacts / f"ocr-{width}x{height}.png"))
            page.locator("#tab-results").click()
            assert page.get_by_text("来源未核实", exact=False).first.is_visible()
            assert not page.locator("#outputs details").first.get_attribute("open")
            page.locator("#tab-results").press("ArrowRight")
            assert page.locator("#tab-history").get_attribute("aria-selected") == "true"
            assert page.locator("#history li").count() == 30
            page.locator("#advanced").click()
            assert page.locator("#advancedPanel").is_visible()
            page.keyboard.press("Escape")
            if width < 900:
                page.locator("#view-files").click()
                assert page.locator("#files").is_visible() and page.locator("#start").is_visible()
                assert not page.locator("#monitor").is_visible()
                page.locator("#view-files").press("ArrowRight")
                assert page.locator("#stop").is_visible()
            print(f"{width}x{height}: {sizes}")

        # Conditional stop identity survives layout changes.
        page.locator("#stop").click()
        page.wait_for_function("!busy && !polling")
        assert parse_qs(requests[-1][0].query) == {
            "expected_instance_id": ["fixture-instance"], "expected_run_id": ["7"]}
        assert requests[-1][2]["x-csrf-token"] == "fixture-csrf"

        # Every directory selection sends explicit names, including select-all;
        # the server allocates a unique output without an absolute path input.
        state.update(running=False, status="idle")
        load(1024, 600)
        assert page.locator("#inputs input:checked").count() == 24
        page.locator("#start").click()
        page.wait_for_function("!busy && !polling")
        assert json.loads(requests[-1][1]) == {
            "pipeline": "paddle", "input_names": [f"原文与研究笔记 {i}.pdf" for i in range(24)]}
        page.locator("#inputs input").first.uncheck()
        page.locator("#start").click()
        page.wait_for_function("!busy && !polling")
        body = json.loads(requests[-1][1])
        assert body == {"pipeline": "paddle", "input_names": [f"原文与研究笔记 {i}.pdf" for i in range(1, 24)]}
        page.locator("#selectAll").check()
        page.locator("#selectAll").uncheck()
        assert page.locator("#start").is_disabled()
        page.locator("#selectAll").check()

        # Unchanged polls must keep actual nodes, keyboard focus and scrolling.
        page.evaluate("""() => {
            window.savedNodes=['inputs','outputs','history','pipeline'].map(id=>$(id).firstChild);
            $('inputs').scrollTop=210;
            $('inputs').querySelectorAll('input')[4].focus({preventScroll:true});
            window.savedInputTop=$('inputs').scrollTop;
        }""")
        refresh()
        assert page.evaluate("""() => ['inputs','outputs','history','pipeline'].every((id,i)=>$(id).firstChild===savedNodes[i])
            && document.activeElement===$('inputs').querySelectorAll('input')[4]
            && $('inputs').scrollTop===savedInputTop""")
        state["inputs"][0]["size"] += 1
        refresh()
        assert page.evaluate("""() => document.activeElement===$('inputs').querySelectorAll('input')[4]
            && $('inputs').scrollTop===savedInputTop""")
        page.locator("#pipeline").focus()
        refresh()
        assert page.locator("#pipeline").evaluate("(node)=>node===document.activeElement")

        # Rebuilt result/history lists restore expanded details, focus and
        # their scrollable parent's position too.
        for tab, list_id, panel_id, data in [("results", "outputs", "panel-results", state["outputs"]),
                                             ("history", "history", "panel-history", state["history"])]:
            page.locator(f"#tab-{tab}").click()
            page.evaluate("""([id,panel])=>{
                const target=$(id);target.querySelector('details').open=true;
                target.querySelectorAll('summary')[4].focus({preventScroll:true});
                $(panel).scrollTop=180;window.savedTop=$(panel).scrollTop;
            }""", [list_id, panel_id])
            refresh()
            assert page.evaluate("panel=>$(panel).scrollTop===savedTop", panel_id)
            if tab == "results":
                data[0]["size"] += 1
            else:
                data[0]["status"] = "interrupted"
            refresh()
            assert page.evaluate("""([id,panel])=>$(''+id).querySelector('details').open
                && document.activeElement===$(id).querySelectorAll('summary')[4]
                && $(panel).scrollTop===savedTop""", [list_id, panel_id])

        page.locator("#tab-logs").click()
        page.evaluate("$('log').scrollTop=66")
        page.wait_for_function("followLog===false")
        before = page.locator("#log").evaluate("(node)=>node.scrollTop")
        log[0] += "[fixture] appended while reading earlier lines\n" * 10
        refresh()
        assert page.locator("#log").evaluate("(node)=>node.scrollTop") == before

        # Log pause leaves status/progress polling live, then resumes from the
        # saved cursor. Hidden documents schedule at least fifteen seconds.
        page.evaluate("$('pauseLog').checked=true")
        old_log = page.locator('#log').inner_text()
        log[0] += '[fixture] received after resuming logs\n'
        state['file_progress'][0]['percent'] = 57
        previous = len(polls)
        refresh()
        assert polls[previous:] == ['status']
        assert page.locator('#log').inner_text() == old_log
        assert '57.0%' in page.locator('#fileProgress').inner_text()
        page.evaluate("$('pauseLog').checked=false")
        refresh()
        assert 'received after resuming' in page.locator('#log').inner_text()
        assert page.evaluate("refreshDelay()") == 1500
        page.evaluate("""() => {
            window.originalTimeout=window.setTimeout;
            window.setTimeout=(callback,delay)=>{window.lastDelay=delay;return originalTimeout(callback,delay);};
            Object.defineProperty(document,'hidden',{configurable:true,value:true});
            document.dispatchEvent(new Event('visibilitychange'));
        }""")
        assert page.evaluate('lastDelay') == 15000
        page.evaluate("""() => {
            Object.defineProperty(document,'hidden',{configurable:true,value:false});
            document.dispatchEvent(new Event('visibilitychange'));
            clearTimeout(refreshTimer);window.setTimeout=originalTimeout;
        }""")
        page.wait_for_function('!polling')
        assert page.evaluate('lastDelay') == 1500
        page.locator('#latestLog').click()
        page.wait_for_function('!polling && followLog')
        log[0] += "[fixture] following latest restored\n" * 10
        refresh()
        assert page.locator('#log').evaluate('(node)=>node.scrollHeight-node.scrollTop-node.clientHeight<2')
        # Stub clipboard writes so browser fixtures never overwrite the user's clipboard.
        page.evaluate("() => {navigator.clipboard.writeText=async text=>{window.copiedLog=text;};}")
        page.locator('#copyLog').click()
        page.wait_for_function("$('logNotice').textContent==='已复制'")
        assert page.evaluate('copiedLog') == page.locator('#log').inner_text()
        page.evaluate("() => {navigator.clipboard.writeText=async()=>{throw Error('fixture permission denied');};}")
        page.locator('#copyLog').click()
        page.wait_for_function("$('logNotice').textContent.includes('复制失败')")

        # A successful poll clears connection errors, not action errors.
        offline[0] = True
        refresh()
        assert "连接失败" in page.locator("#message").inner_text()
        offline[0] = False
        refresh()
        assert page.locator("#message").inner_text() == ""
        action_fail[0] = True
        page.locator("#start").click()
        page.wait_for_function("!busy && !polling")
        assert "fixture conflict" in page.locator("#message").inner_text()
        refresh()
        assert "fixture conflict" in page.locator("#message").inner_text()
        action_fail[0] = False

        # Manual paths live in advanced settings; optional output defaults to
        # the stable service-provided directory, never the previous run path.
        page.locator("#advanced").click()
        page.locator("#sourceMode").select_option("selected")
        page.locator("#inputFiles").fill("/fixture/a.pdf\n/fixture/b.pdf")
        assert page.locator("#outputDir").input_value() == ""
        page.locator("#closeAdvanced").click()
        page.locator("#start").click()
        page.wait_for_function("!busy && !polling")
        body = json.loads(requests[-1][1])
        assert body["input_files"] == ["/fixture/a.pdf", "/fixture/b.pdf"]
        assert body["output_dir"] == "/fixture/output/runs/" + body["run_id"]
        page.locator("#advanced").click()
        page.locator("#sourceMode").select_option("directory")
        page.locator("#outputDir").fill("/fixture/override")
        page.locator("#closeAdvanced").click()
        page.locator("#start").click()
        page.wait_for_function("!busy && !polling")
        assert json.loads(requests[-1][1])["output_dir"] == "/fixture/override"

        # Short screens use document scrolling, so neither section's control
        # is clipped by body overflow. Long titles still stay inside the width.
        state["inputs"][0]["name"] = "很长的中文文献题名与版本说明" * 20 + ".pdf"
        for width, height in [(1024, 400), (390, 420)]:
            load(width, height)
            assert page.evaluate("getComputedStyle(document.body).overflowY") == "auto"
            assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
            if width < 900:
                page.locator("#view-files").click()
            page.locator("#start").scroll_into_view_if_needed()
            assert page.locator("#start").evaluate("(node)=>node.getBoundingClientRect().bottom<=innerHeight")
            assert page.evaluate("document.scrollingElement.scrollTop") > 0
            if width < 900:
                page.locator("#view-monitor").click()
            page.locator("#stop").scroll_into_view_if_needed()
            assert page.locator("#stop").evaluate("(node)=>node.getBoundingClientRect().bottom<=innerHeight")
            page.screenshot(path=str(artifacts / f"ocr-short-{width}x{height}.png"), full_page=True)

        mismatch[0] = True
        page.reload()
        page.wait_for_function("!polling")
        page.evaluate("clearTimeout(refreshTimer)")
        assert page.locator("#stop").is_disabled() and page.locator("#start").is_disabled()
        assert "第 40 行" not in page.locator("#log").inner_text()
        browser.close()
    assert not errors, errors
    print(f"Fixture browser checks passed; screenshots: {artifacts}")


if __name__ == "__main__":
    main()
