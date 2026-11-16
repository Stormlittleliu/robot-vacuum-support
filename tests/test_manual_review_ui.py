# -*- coding: utf-8 -*-
"""Headless-browser regressions for manual-review selection/highlight.

These tests drive the real review_server + review_ui/app.js in headless
Chrome/Edge against a throw-away store under pytest's tmp_path.  They never
write the production ``eval/reviews`` store or the authoritative dataset.

Run with::

    python -m pytest tests/test_manual_review_ui.py -q
"""
from __future__ import annotations

import json
import socket
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

CASE_A = "rsv1-dev-boundary-unknown-model-price-01"   # has runs
CASE_B = "rsv1-dev-boundary-unknown-model-price-02"   # no runs
SCOPE_CASE = "case_definition"


def free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def delay_fetch(cdp, *, method: str, path_contains: str, delay_ms: int) -> None:
    """Delay one matching fetch call inside the page (no server changes)."""
    script = f"""
    (() => {{
      const original = window.fetch;
      if (!original.__mrWrapped) {{
        window.__mrOriginalFetch = original;
        original.__mrWrapped = true;
        window.fetch = function (url, options) {{
          const opts = options || {{}};
          const method = String(opts.method || 'GET').toUpperCase();
          const target = window.__mrDelayTarget;
          const promise = original.apply(this, arguments);
          if (target && String(url).includes(target.path) && method === target.method) {{
            window.__mrDelayTarget = null;
            return promise.then((response) => new Promise((resolve) => setTimeout(() => resolve(response), target.delay)));
          }}
          return promise;
        }};
      }}
      window.__mrDelayTarget = {{method: {json.dumps(method)}, path: {json.dumps(path_contains)}, delay: {int(delay_ms)}}};
      return true;
    }})()
    """
    assert cdp.eval(script)


def select_case(cdp, case_id: str, *, timeout: float = 15.0) -> None:
    cdp.eval(
        f"""(() => {{
          const el = document.querySelector('[data-case-id="{case_id}"]');
          if (!el) return false;
          el.click();
          return true;
        }})()"""
    )
    cdp.wait_js(
        f"window.__reviewSnapshot().selectedCaseId === {json.dumps(case_id)} "
        f"&& window.__reviewSnapshot().panelCaseId === {json.dumps(case_id)} "
        f"&& window.__reviewSnapshot().activeCaseId === {json.dumps(case_id)}",
        timeout=timeout,
        label=f"selected {case_id}",
    )


def assert_selection_consistent(cdp, case_id: str) -> dict:
    snapshot = cdp.eval("window.__reviewSnapshot()")
    active_ids = cdp.eval(
        "Array.from(document.querySelectorAll('#case-list .case-item.active')).map(e => e.dataset.caseId)"
    )
    aria_ids = cdp.eval(
        "Array.from(document.querySelectorAll('#case-list .case-item[aria-current=\"true\"]')).map(e => e.dataset.caseId)"
    )
    assert snapshot["selectedCaseId"] == case_id, snapshot
    assert snapshot["panelCaseId"] == case_id, snapshot
    assert snapshot["activeCaseId"] == case_id, snapshot
    assert active_ids == [case_id], active_ids
    assert aria_ids == [case_id], aria_ids
    return snapshot


def save_current(cdp, status: str, notes: str, revision: int) -> None:
    cdp.eval(
        f"""(() => {{
          const btn = document.querySelector('.status-btn[data-status="{status}"]');
          if (!btn) return false;
          btn.click();
          return true;
        }})()"""
    )
    cdp.eval(
        f"""(() => {{
          const el = document.querySelector('#notes');
          const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
          setter.call(el, {json.dumps(notes)});
          el.dispatchEvent(new Event('input', {{bubbles: true}}));
          return true;
        }})()"""
    )
    cdp.eval("document.querySelector('#save-btn').click(); true")
    cdp.wait_js(
        f"document.querySelector('#save-state').dataset.state === 'saved' "
        f"&& window.__reviewSnapshot().reviewRevision === {revision}",
        timeout=20,
        label=f"saved revision {revision}",
    )


@pytest.fixture
def browser(tmp_path):
    pytest.importorskip("websocket")
    try:
        from eval.review_browser_acceptance import (  # noqa: PLC0415
            CDP,
            chrome_path,
            new_tab,
            start_chrome,
            start_server,
            wait_http,
        )
    except Exception as error:  # pragma: no cover - environment guard
        pytest.skip(f"browser harness unavailable: {error}")
    try:
        chrome_path()
    except Exception as error:  # pragma: no cover - environment guard
        pytest.skip(f"Chrome/Edge not available: {error}")

    port = free_port()
    debug_port = free_port()
    store = tmp_path / "store"
    store.mkdir()
    temp_root = tmp_path / "runtime"
    temp_root.mkdir()
    server = start_server(store, port)
    chrome = None
    cdp = None
    try:
        chrome = start_chrome(temp_root / "chrome-profile", debug_port, temp_root / "chrome.log")
        wait_http(f"http://127.0.0.1:{debug_port}/json/version", timeout=40)
        cdp = CDP(new_tab(debug_port, f"http://127.0.0.1:{port}/"))
        cdp.call("Page.enable")
        cdp.call("Runtime.enable")
        cdp.wait_js("window.__reviewReady === true", timeout=40, label="review app ready")
        cdp.eval("window.confirm = () => true; true")
        yield cdp, store, port
    finally:
        if cdp is not None:
            cdp.close()
        if chrome is not None:
            try:
                chrome.terminate()
                chrome.wait(timeout=10)
            except Exception:
                try:
                    chrome.kill()
                except Exception:
                    pass
        try:
            server.terminate()
            server.wait(timeout=10)
        except Exception:
            try:
                server.kill()
            except Exception:
                pass


def test_highlight_follows_clicked_case_and_late_save_cannot_steal_it(browser):
    cdp, store, _ = browser
    cdp.wait_js("window.__reviewSnapshot().rows === 32", label="32 development rows")

    select_case(cdp, CASE_A)
    save_current(cdp, "passed", "A saved", 1)
    assert_selection_consistent(cdp, CASE_A)

    # A slow detail request must not prevent the list from painting B at once.
    delay_fetch(cdp, method="GET", path_contains=f"/api/cases/{CASE_B}", delay_ms=900)
    select_case(cdp, CASE_B)
    assert_selection_consistent(cdp, CASE_B)
    assert cdp.eval("document.querySelectorAll('#case-list .mini-badge').length >= 2")

    # Now make B dirty, then save A with a deliberately slow POST while B is open.
    cdp.eval(
        """(() => {
          const el = document.querySelector('#notes');
          const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
          setter.call(el, 'B unsaved draft');
          el.dispatchEvent(new Event('input', {bubbles: true}));
          return true;
        })()"""
    )
    b_draft_keys = cdp.eval(
        "Object.keys(localStorage).filter(k => k.startsWith('mr-draft:v1:') && k.includes("
        + json.dumps(CASE_B)
        + "))"
    )
    assert len(b_draft_keys) == 1, b_draft_keys
    b_draft_before = cdp.eval("localStorage.getItem(" + json.dumps(b_draft_keys[0]) + ")")
    assert "B unsaved draft" in b_draft_before

    select_case(cdp, CASE_A)
    cdp.eval("document.querySelector('.status-btn[data-status=\"failed\"]').click(); true")
    cdp.eval(
        """(() => {
          const el = document.querySelector('#notes');
          const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
          setter.call(el, 'A late save');
          el.dispatchEvent(new Event('input', {bubbles: true}));
          return true;
        })()"""
    )
    delay_fetch(cdp, method="POST", path_contains="/api/reviews", delay_ms=1600)
    cdp.eval("document.querySelector('#save-btn').click(); true")
    time.sleep(0.15)
    select_case(cdp, CASE_B)
    cdp.wait_js("window.__reviewSnapshot().panelCaseId === " + json.dumps(CASE_B), label="B panel after switch")

    # While A's POST is still in flight, B's own draft must be protected.
    cdp.eval("document.querySelector('.status-btn[data-status=\"uncertain\"]').click(); true")
    cdp.eval(
        """(() => {
          const el = document.querySelector('#notes');
          const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
          setter.call(el, 'B unsaved draft v2');
          el.dispatchEvent(new Event('input', {bubbles: true}));
          return true;
        })()"""
    )
    cdp.wait_js(
        "document.querySelector('#toast').textContent.includes('当前选择未被旧回调覆盖')",
        timeout=8,
        label="A save settled without stealing B",
    )
    time.sleep(0.2)
    after = assert_selection_consistent(cdp, CASE_B)
    assert after["dirty"] is True, after
    assert after["reviewRevision"] is None, after
    assert after["notes"] == "B unsaved draft v2", after
    b_draft_after = cdp.eval("localStorage.getItem(" + json.dumps(b_draft_keys[0]) + ")")
    assert b_draft_after is not None and "B unsaved draft v2" in b_draft_after

    a_record = cdp.eval(
        "fetch('/api/reviews?case_id=' + encodeURIComponent("
        + json.dumps(CASE_A)
        + ") + '&scope=case_definition').then(r => r.json())",
        await_promise=True,
    )
    assert a_record["current"]["status"] == "failed"
    assert a_record["current"]["notes"] == "A late save"
    assert cdp.eval("window.__reviewErrors.length") == 0
    saved_files = list((store / "reviews" / "reviews").glob("*.json"))
    assert len(saved_files) == 1  # same review target keeps one file, revision 2
    saved_record = json.loads(saved_files[0].read_text(encoding="utf-8"))
    assert saved_record["caseId"] == CASE_A
    assert saved_record["revision"] == 2


def test_filter_scope_run_and_stale_detail_keep_selection_consistent(browser):
    cdp, store, _ = browser
    cdp.wait_js("window.__reviewSnapshot().rows === 32", label="32 development rows")
    cache_headers = cdp.eval(
        "Promise.all([fetch('/'), fetch('/static/app.js')]).then(rs => rs.map(r => r.headers.get('cache-control')))",
        await_promise=True,
    )
    assert cache_headers == ["no-store", "no-store"], cache_headers

    select_case(cdp, CASE_A)
    save_current(cdp, "passed", "A passed for filter", 1)

    # Hide B under the "passed" filter: the UI must move highlight + panel to
    # the first visible case together, never leave them pointing at different cases.
    select_case(cdp, CASE_B)
    cdp.eval(
        """(() => {
          const el = document.querySelector('#filter');
          el.value = 'passed';
          el.dispatchEvent(new Event('change', {bubbles: true}));
          return true;
        })()"""
    )
    cdp.wait_js(
        "window.__reviewSnapshot().rows === 1 && window.__reviewSnapshot().selectedCaseId === "
        + json.dumps(CASE_A),
        label="filter hides B and selects A",
    )
    assert_selection_consistent(cdp, CASE_A)

    # Dirty draft + filter switch: confirm=true means the switch proceeds, the
    # hidden case's draft must survive.
    cdp.eval(
        """(() => {
          const el = document.querySelector('#filter');
          el.value = 'all';
          el.dispatchEvent(new Event('change', {bubbles: true}));
          return true;
        })()"""
    )
    cdp.wait_js("window.__reviewSnapshot().rows === 32", label="all rows")
    select_case(cdp, CASE_B)
    cdp.eval(
        """(() => {
          const el = document.querySelector('#notes');
          const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
          setter.call(el, 'B draft hidden by filter');
          el.dispatchEvent(new Event('input', {bubbles: true}));
          return true;
        })()"""
    )
    cdp.eval(
        """(() => {
          const el = document.querySelector('#filter');
          el.value = 'passed';
          el.dispatchEvent(new Event('change', {bubbles: true}));
          return true;
        })()"""
    )
    cdp.wait_js(
        "window.__reviewSnapshot().rows === 1 && window.__reviewSnapshot().selectedCaseId === "
        + json.dumps(CASE_A),
        label="dirty filter switch selects A",
    )
    assert_selection_consistent(cdp, CASE_A)
    assert cdp.eval(
        "Object.keys(localStorage).some(k => k.startsWith('mr-draft:v1:') && k.includes("
        + json.dumps(CASE_B)
        + "))"
    )

    # Scope switch keeps the current visible case and reloads it; highlight,
    # aria-current and main panel must still agree.
    cdp.eval(
        """(() => {
          const el = document.querySelector('#filter');
          el.value = 'all';
          el.dispatchEvent(new Event('change', {bubbles: true}));
          return true;
        })()"""
    )
    cdp.wait_js("window.__reviewSnapshot().rows === 32", label="all rows again")
    select_case(cdp, CASE_B)
    cdp.eval("document.querySelector('#scope-run_answer').click(); true")
    cdp.wait_js(
        "window.__reviewSnapshot().scope === 'run_answer' && window.__reviewSnapshot().selectedCaseId === "
        + json.dumps(CASE_B),
        label="scope switch keeps B",
    )
    assert_selection_consistent(cdp, CASE_B)

    # Run switch must not change the selected case.
    select_case(cdp, CASE_A)
    cdp.wait_js(
        "window.__reviewSnapshot().scope === 'run_answer' && window.__reviewSnapshot().selectedCaseId === "
        + json.dumps(CASE_A)
        + " && !!document.querySelector('#run-select')",
        label="run scope A",
    )
    runs = cdp.eval(
        "fetch('/api/cases/' + encodeURIComponent("
        + json.dumps(CASE_A)
        + ")).then(r => r.json()).then(d => d.runs.map(r => r.runId))",
        await_promise=True,
    )
    assert len(runs) >= 2, runs
    target_run = runs[1]
    cdp.eval(
        f"""(() => {{
          const el = document.querySelector('#run-select');
          el.value = {json.dumps(target_run)};
          el.dispatchEvent(new Event('change', {{bubbles: true}}));
          return el.value;
        }})()"""
    )
    cdp.wait_js(
        "window.__reviewSnapshot().runId === " + json.dumps(target_run),
        label="run switch",
    )
    assert_selection_consistent(cdp, CASE_A)

    # A stale detail response for A must not overwrite B.
    select_case(cdp, CASE_B)
    delay_fetch(cdp, method="GET", path_contains=f"/api/cases/{CASE_A}", delay_ms=1200)
    cdp.eval(
        f"""(() => {{
          const el = document.querySelector('[data-case-id="{CASE_A}"]');
          el.click();
          return true;
        }})()"""
    )
    time.sleep(0.1)
    cdp.eval(
        f"""(() => {{
          const el = document.querySelector('[data-case-id="{CASE_B}"]');
          el.click();
          return true;
        }})()"""
    )
    cdp.wait_js(
        "window.__reviewSnapshot().selectedCaseId === " + json.dumps(CASE_B)
        + " && window.__reviewSnapshot().panelCaseId === " + json.dumps(CASE_B),
        label="B survives stale A detail",
    )
    time.sleep(1.4)
    assert_selection_consistent(cdp, CASE_B)
    assert cdp.eval("window.__reviewErrors.length") == 0
    assert len(list((store / "reviews" / "reviews").glob("*.json"))) == 1
