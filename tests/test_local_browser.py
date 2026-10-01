"""Offline DOM tests in installed Chrome; fresh retained profile, no personal tabs."""
from contextlib import nullcontext
import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time

import pytest
import playwright.sync_api as playwright_sync_api
from playwright.sync_api import sync_playwright

from test_reliability import Clock, current_html, legacy_html, manual_binding


@pytest.fixture
def fixture_adapters(engine, monkeypatch):
    # Synthetic contracts only. Never register these as live ChatGPT support.
    for kind in ('current', 'legacy', 'legacy_slider'):
        monkeypatch.setitem(engine._DISPATCH_ADAPTERS, kind,
            dict(click=True, enter=True, evidence='offline fixture only'))
        monkeypatch.setitem(engine._ATTACHMENT_ADAPTERS, kind,
            dict(chip='[data-attachment-id]', identity='data-attachment-id',
                 names=['text', 'title', 'aria-label'], ready='[data-ready="true"]',
                 progress='[role="progressbar"]', evidence='offline fixture only'))


@pytest.fixture(scope='module')
def local_browser():
    executable = Path('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    if not executable.is_file():
        pytest.skip('installed macOS Chrome unavailable; no download/install')
    root = Path(tempfile.mkdtemp(prefix='insane-review-local-dom-'))
    profile = root / 'profile'
    profile.mkdir(mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    with os.fdopen(os.open(root / 'chrome.log', flags, 0o600), 'wb') as log:
        proc = subprocess.Popen([str(executable), '--headless=new', '--no-first-run',
            '--no-default-browser-check', '--disable-background-networking', '--disable-sync',
            '--disable-component-update', '--disable-extensions', '--metrics-recording-only',
            '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=0',
            f'--user-data-dir={profile}', 'about:blank'], stdout=log, stderr=log,
            start_new_session=True)
        try:
            deadline = time.monotonic() + 20
            port_file = profile / 'DevToolsActivePort'
            while not port_file.exists() and time.monotonic() < deadline and proc.poll() is None:
                time.sleep(0.1)
            assert port_file.exists(), f'isolated Chrome CDP unavailable; retained log: {root}'
            port = int(port_file.read_text().splitlines()[0])
            with sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp(f'http://127.0.0.1:{port}')
                try:
                    yield browser
                finally:
                    browser.close()
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=10)
    # Keep profile/log artifacts. Playwright did not launch/manage this profile.


@pytest.fixture
def local_page(local_browser):
    context = local_browser.new_context(offline=True, service_workers='block')
    context.route('**/*', lambda route: route.abort())
    page = context.new_page()
    page.set_default_timeout(2000)
    try:
        yield page
    finally:
        context.close()


def mode_html(chat_pressed='false', work_pressed='true', label='작성기 모드'):
    return f'''<div role="group" aria-label="{label}" id="mode-group">
      <button aria-pressed="{chat_pressed}" onclick="window.modeClicks++;this.setAttribute('aria-pressed','true');document.querySelector('[data-mode=work]').setAttribute('aria-pressed','false')">Chat</button>
      <button data-mode="work" aria-pressed="{work_pressed}" onclick="window.modeClicks++;this.setAttribute('aria-pressed','true');document.querySelector('#mode-group button').setAttribute('aria-pressed','false')">Work</button>
    </div><script>window.modeClicks=0;</script>'''


@pytest.mark.parametrize('label', ['작성기 모드', 'Composer mode', 'localized header'])
def test_current_mode_group_read_and_same_group_correction(engine, local_page, monkeypatch, label):
    local_page.set_content(mode_html(label=label))
    monkeypatch.setattr(engine, 'time', Clock())
    assert engine.read_mode(local_page) == 'work'
    assert engine.ensure_chat_mode(local_page) == (True, 'chat')
    assert local_page.eval_on_selector('#mode-group button', "b => b.getAttribute('aria-pressed')") == 'true'
    assert local_page.evaluate('window.modeClicks') == 1


def test_legacy_mode_radio_still_reads_and_corrects(engine, local_page, monkeypatch):
    local_page.set_content('''<div role="radiogroup" id="legacy-mode">
      <button role="radio" aria-checked="false" onclick="window.modeClicks++;this.setAttribute('aria-checked','true');document.querySelector('[data-mode=work]').setAttribute('aria-checked','false')">Chat</button>
      <button role="radio" data-mode="work" aria-checked="true" onclick="window.modeClicks++">Work</button>
    </div><script>window.modeClicks=0;</script>''')
    monkeypatch.setattr(engine, 'time', Clock())
    assert engine.read_mode(local_page) == 'work'
    assert engine.ensure_chat_mode(local_page) == (True, 'chat')
    assert local_page.evaluate('window.modeClicks') == 1


def test_absent_mode_is_only_confirmed_after_successful_scan(engine, local_page):
    local_page.set_content('<main>no mode controls</main>')
    assert engine.read_mode(local_page) == 'absent'
    assert engine.ensure_chat_mode(local_page) == (True, 'absent')
    assert engine.mode_probe_value(engine.read_mode(local_page)) == 'none'


def test_malformed_current_mode_blocks_legacy_fallback_and_click(engine, local_page):
    html = mode_html(chat_pressed='false', work_pressed='false') + '''
      <div role="radiogroup"><div role="radio" aria-checked="false">Chat</div>
      <div role="radio" aria-checked="true">Work</div></div>'''
    local_page.set_content(html)
    assert engine.read_mode(local_page) == 'unknown'
    assert engine.ensure_chat_mode(local_page) == (False, 'unknown')
    assert local_page.evaluate('window.modeClicks') == 0


@pytest.mark.parametrize('duplicate', ['buttons', 'groups'])
def test_duplicate_current_mode_candidates_block_without_click(engine, local_page, duplicate):
    html = mode_html()
    if duplicate == 'buttons':
        html = html.replace('</div><script>',
            '<button aria-pressed="false" onclick="window.modeClicks++">Chat</button></div><script>')
    else:
        html += '''<div role="group" aria-label="Composer mode" id="second-mode-group">
          <button aria-pressed="false" onclick="window.modeClicks++">Chat</button>
          <button aria-pressed="true" onclick="window.modeClicks++">Work</button>
        </div>'''
    local_page.set_content(html)
    assert engine.read_mode(local_page) == 'unknown'
    assert engine.ensure_chat_mode(local_page) == (False, 'unknown')
    assert local_page.evaluate('window.modeClicks') == 0


@pytest.mark.parametrize('observation', ['exception', 'malformed'])
def test_probe_login_reports_unknown_mode_when_observation_fails(
        engine, local_page, monkeypatch, observation):
    if observation == 'malformed':
        # A current-mode candidate with contradictory state is malformed, not absent.
        local_page.set_content(mode_html(chat_pressed='false', work_pressed='false'))
    else:
        local_page.set_content(mode_html())
        evaluate = local_page.evaluate

        def fail_mode_read(script, *args, **kwargs):
            if script == engine.JS_READ_MODE:
                raise RuntimeError('PRIVATE_PROBE_MODE_EXCEPTION_MARKER')
            return evaluate(script, *args, **kwargs)

        monkeypatch.setattr(local_page, 'evaluate', fail_mode_read)

    class FakePlaywright:
        def __enter__(self):
            return object()

        def __exit__(self, *_args):
            return False

    class FakeContext:
        def cookies(self, _url):
            return []

        def new_page(self):
            return local_page

    monkeypatch.setattr(engine, 'is_port_open', lambda _port: True)
    monkeypatch.setattr(engine, 'cdp_browser_ok', lambda: True)
    monkeypatch.setattr(importlib.util, 'find_spec', lambda _name: object())
    monkeypatch.setattr(engine, 'sync_playwright', FakePlaywright)
    monkeypatch.setattr(playwright_sync_api, 'sync_playwright', FakePlaywright)
    monkeypatch.setattr(engine, 'connect_cdp', lambda _pw: object())
    monkeypatch.setattr(engine, 'pick_context', lambda _browser: FakeContext())
    monkeypatch.setattr(engine, 'hide_browser_if_background', lambda: None)
    monkeypatch.setattr(engine, '_guard_dialogs', lambda *_args: None)
    monkeypatch.setattr(engine, 'login_state', lambda *_args, **_kwargs: 'ok')
    monkeypatch.setattr(local_page, 'goto', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(local_page, 'close', lambda: None)
    monkeypatch.setattr(engine, 'time', Clock())

    result = engine.probe_login()
    assert result['login'] == 'ok'
    assert result['mode'] == 'unknown'


def test_initial_mode_read_exception_blocks_without_click(engine, local_page, monkeypatch):
    local_page.set_content(mode_html())
    evaluate = local_page.evaluate
    def fail_read(script, *args, **kwargs):
        if script == engine.JS_READ_MODE:
            raise RuntimeError('PRIVATE_MODE_EXCEPTION_MARKER')
        if script == engine.JS_CLICK_MODE:
            pytest.fail('unknown mode must not click')
        return evaluate(script, *args, **kwargs)
    monkeypatch.setattr(local_page, 'evaluate', fail_read)
    assert engine.read_mode(local_page) == 'unknown'
    assert engine.ensure_chat_mode(local_page) == (False, 'unknown')
    assert engine.mode_probe_value(engine.read_mode(local_page)) == 'unknown'
    assert local_page.evaluate('window.modeClicks') == 0


def test_failed_post_correction_read_never_confirms_chat(engine, local_page, monkeypatch):
    local_page.set_content(mode_html())
    evaluate = local_page.evaluate
    reads = 0
    def fail_after_click(script, *args, **kwargs):
        nonlocal reads
        if script == engine.JS_READ_MODE:
            reads += 1
            if reads > 1:
                raise RuntimeError('PRIVATE_POST_CLICK_READ_MARKER')
        return evaluate(script, *args, **kwargs)
    monkeypatch.setattr(local_page, 'evaluate', fail_after_click)
    monkeypatch.setattr(engine, 'time', Clock())
    assert engine.ensure_chat_mode(local_page) == (False, 'unknown')
    assert local_page.evaluate('window.modeClicks') == 1


@pytest.mark.parametrize('extra,valid', [
    ('<pre><button type="button" aria-label="복사">code</button></pre>', True),
    ('user_copy', True),
    ('<button type="button" aria-label="복사">second turn copy</button>', False),
    ('code_only', False),
])
def test_actual_js_copy_filter(engine, local_page, extra, valid):
    page = local_page
    page.set_content(current_html())
    if extra == 'code_only':
        page.eval_on_selector('[aria-label="복사"]', "b => {const pre=document.createElement('pre'); b.replaceWith(pre); pre.append(b);}")
    elif extra == 'user_copy':
        page.eval_on_selector('[data-chatgpt-search-unit-key$=user]',
            "el => el.insertAdjacentHTML('beforeend', '<button type=button aria-label=복사>user copy</button>')")
    else:
        page.eval_on_selector('[data-turn-key]', '(el, html) => el.insertAdjacentHTML("beforeend", html)', extra)
    snapshot = engine.response_snapshot(page, manual_binding(page))
    assert (snapshot is not None) is valid


def test_real_dispatch_ack_failure_never_reclicks(engine, local_page, monkeypatch, fixture_adapters):
    from types import SimpleNamespace
    page = local_page
    page.set_content(current_html() + '<button data-testid="send-button" onclick="window.sent++">Send</button>')
    page.evaluate("() => {window.sent=0;window.enters=0;document.addEventListener('keydown', e => {if(e.key==='Enter') window.enters++;});}")
    editor = engine.active_composer(page)
    editor.fill('prompt')
    evaluate = page.evaluate
    def lost_ack(script, *args):
        result = evaluate(script, *args)
        if 'sendSelector' in script:
            raise OSError('ack lost after actual DOM dispatch')
        return result
    monkeypatch.setattr(page, 'evaluate', lost_ack)
    monkeypatch.setattr(engine, 'time', Clock())
    with pytest.raises(OSError):
        engine.click_send(page, 'prompt', editor)
    assert evaluate('() => [window.sent,window.enters]') == [1, 0]


@pytest.mark.parametrize('other_slider', [False, True])
def test_actual_js_legacy_slider_outside_picker(engine, local_page, monkeypatch, other_slider):
    page = local_page
    page.set_content(legacy_html(True).replace('data-testid="composer-intelligence-picker-content"', ''))
    page.eval_on_selector('[role="slider"]', "s => s.setAttribute('aria-valuenow','2')")
    page.eval_on_selector('#trigger', "b => b.innerText='High'")
    page.eval_on_selector('[data-model-reasoning-effort-slider]', """el => {
        el.tabIndex=0;
        el.addEventListener('keydown', e => {
            const s=el.querySelector('[role=slider]');
            if (e.key==='ArrowRight') s.setAttribute('aria-valuenow', +s.getAttribute('aria-valuenow')+1);
            if (e.key==='ArrowLeft') s.setAttribute('aria-valuenow', +s.getAttribute('aria-valuenow')-1);
            document.querySelector('#trigger').innerText=['Instant','Medium','High','Extra High','Pro'][+s.getAttribute('aria-valuenow')];
        });
    }""")
    monkeypatch.setattr(engine, 'time', Clock())
    if other_slider:
        page.evaluate("""() => document.body.insertAdjacentHTML('beforeend',
            '<div data-reasoning-slider tabindex="0"><span role="slider" aria-valuemin="0" aria-valuenow="1" aria-valuemax="4"></span></div>')""")
    scope = engine.selection_state(page)['menu']
    assert engine.selection_state(page)['slider'] == (0, 2, 4)
    assert engine._slider_value(page, scope) == (0, 2, 4)
    assert engine._set_effort_slider(page, 4, scope)
    assert engine._slider_value(page, scope) == (0, 4, 4)
    assert engine.selection_state(page)['effort'] == 'pro'
    assert page.eval_on_selector('[data-model-reasoning-effort-slider]', 'el => document.activeElement === el')
    if other_slider:
        assert page.get_attribute('[data-reasoning-slider] [role=slider]', 'aria-valuenow') == '1'


@pytest.mark.parametrize('kind', ['current', 'legacy'])
@pytest.mark.parametrize('location', ['inside', 'outside', 'editor', 'hidden'])
@pytest.mark.parametrize('structure', ['flat', 'nested', 'formless'])
def test_real_file_input_scoped_attachment(engine, local_page, tmp_path, monkeypatch, fixture_adapters, kind, location, structure):
    path = tmp_path / 'synthetic-review-source.md'
    path.write_text('synthetic fixture only')
    attrs = 'role="textbox" data-composer-markdown' if kind == 'current' else 'id="prompt-textarea"'
    editor = f'<div contenteditable="true" {attrs}>prompt</div>'
    if structure == 'nested':
        editor = f'<div role="presentation">{editor}</div>'
    tag = 'div role="presentation"' if structure == 'formless' else 'form'
    end = 'div' if structure == 'formless' else 'form'
    local_page.set_content(f'<{tag} id="attachment-scope">{editor}<input type="file"></{end}>')
    local_page.evaluate("""({name, location}) => {
        document.querySelector('input').addEventListener('change', () => {
            const chip=document.createElement('span'); chip.textContent=name;
            chip.dataset.attachmentId='new1';chip.dataset.ready='true';
            if(location==='hidden') chip.style.display='none';
            const scope=location==='outside' ? document.body : location==='editor' ? document.querySelector('[contenteditable]') : document.querySelector('#attachment-scope');
            scope.append(chip);
        });
    }""", {'name': path.name, 'location': location})
    monkeypatch.setattr(engine, 'time', Clock())
    result = engine.attach_file(local_page, path)
    assert (result['state'] == 'confirmed') is (location == 'inside')
    assert not result['fallback_allowed']
    assert local_page.eval_on_selector('input', 'el => el.files.length') == 1


@pytest.mark.parametrize('kind', ['current', 'legacy'])
@pytest.mark.parametrize('input_count', [0, 2])
def test_attachment_form_input_uniqueness_never_falls_back(engine, local_page, tmp_path, monkeypatch, fixture_adapters, kind, input_count):
    path = tmp_path / 'synthetic-review-source.md'
    path.write_text('synthetic fixture only')
    attrs = 'role="textbox" data-composer-markdown' if kind == 'current' else 'id="prompt-textarea"'
    inner_input = '<input type="file">' if input_count == 2 else ''
    outer_input = '<input type="file">' if input_count == 2 else ''
    local_page.set_content(f'<form><div role="presentation"><div contenteditable="true" {attrs}>prompt</div>'
                          f'{inner_input}<span>{path.name}</span></div>{outer_input}</form><input type="file">')
    monkeypatch.setattr(engine, 'time', Clock())
    assert engine.attach_file(local_page, path)['state'] == 'not_attempted'
    assert local_page.eval_on_selector_all('input', 'els => els.every(el => el.files.length === 0)')


@pytest.mark.parametrize('anomaly', ['unknown', 'streaming'])
def test_real_snapshot_status_observation_resets_stability(engine, local_page, monkeypatch, capsys, anomaly):
    page = local_page
    page.set_content(current_html())
    clock = Clock()
    monkeypatch.setattr(engine, 'time', clock)
    original = engine.streaming_state
    calls_at_30 = []
    def streaming(p):
        if clock.now >= 25:
            p.eval_on_selector('[data-chatgpt-search-unit-key$=assistant]', "el => el.innerText='changed at 25s'")
        if clock.now == 30:
            calls_at_30.append(1)
            if len(calls_at_30) == 3:  # snapshot + terminal + final observation/status
                return anomaly
        return original(p)
    monkeypatch.setattr(engine, 'streaming_state', streaming)
    saves = []
    result = engine.wait_for_turn_response(page, max_wait=50, binding=manual_binding(page),
        save_response=lambda *a: saves.append(clock.now) or True)
    assert result[0] == 'ok' and saves == [38.5]
    assert f'30s | phase=ASSISTANT_BOUND | streaming={anomaly} | terminal=n' in capsys.readouterr().out


@pytest.mark.parametrize('surface', [
    '<div role="alert">Something went wrong</div>',
    '<button data-testid="login-button">Log in</button>',
])
@pytest.mark.parametrize('bound', [False, True])
def test_real_visible_error_stops_bound_and_unbound(engine, local_page, monkeypatch, surface, bound):
    page = local_page
    page.set_content(current_html() + surface)
    clock = Clock()
    monkeypatch.setattr(engine, 'time', clock)
    saves = []
    binding = manual_binding(page) if bound else {'phase': 'SEND_PENDING'}
    assert engine.wait_for_turn_response(page, max_wait=120, binding=binding,
        save_response=lambda *a: saves.append(1))[0] == 'error'
    assert clock.now == 3 and not saves


@pytest.mark.parametrize('surface', [
    '<div role="alert" style="display:none">Something went wrong</div>',
    '<div role="alert"></div>',
    '<div role="dialog">Upload complete</div>',
])
def test_real_benign_surface_does_not_block(engine, local_page, surface):
    local_page.set_content(current_html() + surface)
    assert engine.error_surface_state(local_page) == 'clear'
    assert engine.response_snapshot(local_page, manual_binding(local_page)) is not None
