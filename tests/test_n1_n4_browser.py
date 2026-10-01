import hashlib
import json
import sys
from types import SimpleNamespace

import pytest

from test_local_browser import local_browser, local_page, fixture_adapters, mode_html
from test_reliability import Clock, Page, current_html


def composer_html(kind, button=True):
    attrs = 'role="textbox" data-composer-markdown' if kind == 'current' else 'id="prompt-textarea"'
    return (f'<form><div contenteditable="true" {attrs}>initial</div>'
            + ('<button type="submit" data-testid="send-button">Send</button>' if button else '') + '</form>')


def event_counters(page):
    page.evaluate("""() => {
        window.events={click:0,enter:0,submit:0};
        document.querySelector('form').addEventListener('submit', e => {e.preventDefault();events.submit++;});
        document.addEventListener('click', () => events.click++);
        document.querySelector('[contenteditable]').addEventListener('keydown', e => {
            if(e.key==='Enter'){events.enter++;e.preventDefault();e.target.closest('form').requestSubmit();}
        });
    }""")


@pytest.mark.parametrize('kind', ['current', 'legacy'])
@pytest.mark.parametrize('mode', ['click', 'enter'])
@pytest.mark.parametrize('draft', ['prefix expected', 'expected suffix'])
def test_exact_body_rejects_draft_dispatch(engine, local_page, monkeypatch, fixture_adapters, kind, mode, draft):
    local_page.set_content(composer_html(kind, mode == 'click'))
    event_counters(local_page)
    editor = engine.active_composer(local_page)
    editor.fill(draft)
    monkeypatch.setattr(engine, 'time', Clock())
    assert not engine.composer_has_prompt(local_page, 'expected', editor)
    with pytest.raises(RuntimeError, match='불일치'):
        engine.click_send(local_page, 'expected', editor)
    assert local_page.evaluate('events') == dict(click=0, enter=0, submit=0)


@pytest.mark.parametrize('kind', ['current', 'legacy'])
def test_same_editor_write_read_clear_ignores_unrelated(engine, local_page, kind):
    local_page.set_content('<div contenteditable="true" id="unrelated">untouched</div>' + composer_html(kind))
    editor = engine.active_composer(local_page)
    engine.put_text(local_page, 'expected', editor)
    assert engine.read_composer_text(local_page, editor) == 'expected'
    engine.clear_composer(local_page, editor)
    assert engine.normalize(engine.read_composer_text(local_page, editor)) == ''
    assert local_page.inner_text('#unrelated') == 'untouched'


def test_hidden_legacy_active_current(engine, local_page):
    local_page.set_content('<div id="prompt-textarea" hidden contenteditable="true">old</div>' + composer_html('current'))
    editor = engine.active_composer(local_page)
    engine.put_text(local_page, 'expected', editor)
    assert engine.composer_has_prompt(local_page, 'expected', editor)
    assert local_page.text_content('#prompt-textarea') == 'old'


def test_mutation_during_real_click_actionability_wait(engine, local_page, fixture_adapters):
    page = local_page
    page.set_content(composer_html('current') + '<div id="overlay" style="position:fixed;inset:0;z-index:999;background:white"></div>')
    event_counters(page)
    editor = engine.put_text(page, 'expected')
    page.evaluate("""() => setTimeout(() => {
        document.querySelector('[contenteditable]').innerText='changed while trial click waited';
        document.querySelector('#overlay').style.display='none';
    }, 200)""")
    with pytest.raises(RuntimeError, match='불일치'):
        engine.click_send(page, 'expected', editor)
    assert page.evaluate('events') == dict(click=0, enter=0, submit=0)


@pytest.mark.parametrize('mutation', ['body', 'focus'])
def test_enter_focus_preparation_cannot_bypass_guard(engine, local_page, monkeypatch, fixture_adapters, mutation):
    page = local_page
    page.set_content(composer_html('current', False) + '<input id="other">')
    event_counters(page)
    editor = engine.put_text(page, 'expected')
    page.focus('#other')
    page.evaluate("""mode => document.querySelector('[contenteditable]').addEventListener('focus', e => {
        if(mode==='body') e.target.innerText='changed on focus';
        else document.querySelector('#other').focus();
    })""", mutation)
    monkeypatch.setattr(engine, 'time', Clock())
    with pytest.raises(RuntimeError, match='불일치'):
        engine.click_send(page, 'expected', editor)
    assert page.evaluate('events') == dict(click=0, enter=0, submit=0)


@pytest.mark.parametrize('mode', ['click', 'enter'])
def test_fixture_guarded_positive_control(engine, local_page, monkeypatch, fixture_adapters, mode):
    local_page.set_content(composer_html('current', mode == 'click'))
    event_counters(local_page)
    editor = engine.put_text(local_page, 'expected')
    monkeypatch.setattr(engine, 'time', Clock())
    assert engine.click_send(local_page, 'expected', editor)
    assert local_page.evaluate('events.submit') == 1


def test_current_accessible_dispatch_enabled_but_legacy_stays_unsupported(engine, local_page, tmp_path):
    local_page.set_content(composer_html('current'))
    event_counters(local_page)
    editor = engine.put_text(local_page, 'expected')
    assert engine.click_send(local_page, 'expected', editor)
    assert local_page.evaluate('events.submit') == 1

    local_page.set_content(composer_html('legacy') + '<input type="file">')
    event_counters(local_page)
    editor = engine.put_text(local_page, 'expected')
    with pytest.raises(RuntimeError, match='unsupported'):
        engine.click_send(local_page, 'expected', editor)
    result = engine.attach_file(local_page, tmp_path / 'not-read.txt')
    assert result == dict(state='not_attempted', fallback_allowed=False, reason='unsupported')
    assert local_page.evaluate('events.submit') == 0


# 실측(2026-10-01): 업로드 메뉴는 폼 밖 BODY 아래 DIV이며 "+" 버튼과 left가 같고 바로 인접해 열린다.
PLACE_MENU_JS = """() => {
    window.placeMenu = node => {
        const plus = document.querySelector('[aria-label="파일 등 추가"]');
        const r = plus.getBoundingClientRect();
        let menu = node;
        if (node.tagName === 'BUTTON') { menu = document.createElement('div'); menu.append(node); }
        if (menu.querySelectorAll('button').length < 2) {
            const filler = document.createElement('button'); filler.textContent = 'Add Space files'; menu.append(filler);
        }
        menu.style.cssText = `position:absolute;left:${r.left + scrollX}px;top:${r.bottom + scrollY + 8}px`;
        document.body.append(menu);
        return menu;
    };
}"""


def accessible_attachment_card(name, progress=False):
    status = f'<span>{name} 업로드 중</span>' if progress else ''
    return (f'<div class="file-card"><button type="button">{name}</button>'
            f'<button type="button" aria-label="{name} 제거">×</button>{status}</div>')


def test_current_accessible_attachment_waits_for_stable_ready_state(engine, local_page, tmp_path,
                                                                    monkeypatch):
    page = local_page
    path = tmp_path / 'pack_unique-review-run.md'
    path.write_text('synthetic pack')
    html = composer_html('current').replace('<button type="submit"', '<button disabled type="submit"')
    page.set_content(html.replace('</form>', '<input type="file" hidden>'
                                   '<button type="button" aria-label="파일 등 추가">+</button></form>'))
    page.evaluate(PLACE_MENU_JS)
    page.evaluate("""name => {
        window.uploads=0;
        const input = document.querySelector('input[type=file]');
        document.querySelector('[aria-label="파일 등 추가"]').addEventListener('click', e => {
            e.currentTarget.setAttribute('aria-expanded', 'true');  // 실측: 메뉴가 열리면 true
            const action = document.createElement('button');
            action.setAttribute('aria-label', '사진 및 파일 추가 컴퓨터에서 업로드');
            action.textContent = '사진 및 파일 추가 컴퓨터에서 업로드';
            action.addEventListener('click', () => input.click()); window.placeMenu(action);
        });
        input.addEventListener('change', () => {
            uploads++;
            const card=document.createElement('div');card.className='file-card';
            const file=document.createElement('button');file.type='button';file.textContent=name;
            const remove=document.createElement('button');remove.type='button';remove.textContent='×';
            remove.setAttribute('aria-label', `${name} 제거`);
            const status=document.createElement('span');status.textContent=`${name} 업로드 중`;
            card.append(file, remove, status);document.querySelector('form').append(card);
            window.finishUpload=()=>{status.remove();document.querySelector('[data-testid=send-button]').disabled=false;};
        });
    }""", path.name)
    clock = Clock()
    def sleep(seconds):
        clock.now += seconds
        if clock.now >= 1:
            page.evaluate('window.finishUpload && window.finishUpload()')
    clock.sleep = sleep
    monkeypatch.setattr(engine, 'time', clock)

    result = engine.attach_file(page, path)

    assert result == dict(state='confirmed', fallback_allowed=False,
                          reason='new_accessible_attachment_ready',
                          identity=path.name, filename=path.name)
    assert clock.now >= 2
    assert page.evaluate('window.uploads') == 1


@pytest.mark.parametrize('action_name', [
    '사진 및 파일 추가 컴퓨터에서 업로드', '사진 및 파일 업로드',
    '파일 업로드', 'Upload from computer', 'Choose files from your computer',
])
@pytest.mark.parametrize('clear_input_after_change', [False, True])
def test_current_attachment_uses_visible_add_menu_and_native_file_chooser(
        engine, local_page, tmp_path, monkeypatch, action_name, clear_input_after_change):
    page = local_page
    path = tmp_path / 'pack_menu-review-run.md'
    path.write_text('synthetic pack')
    html = composer_html('current').replace('<button type="submit"',
                                             '<button disabled type="submit"')
    page.set_content(html.replace('</form>',
        '<input type="file" hidden><input type="file" hidden>'
        '<button type="button" aria-label="파일 등 추가">+</button></form>'))
    page.evaluate(PLACE_MENU_JS)
    page.evaluate("""({name, label, clearInputAfterChange}) => {
        window.uploads = 0;
        document.querySelector('[aria-label="파일 등 추가"]').addEventListener('click', e => {
            e.currentTarget.setAttribute('aria-expanded', 'true');  // 실측: 메뉴가 열리면 true
            const menu = document.createElement('div'); menu.className = 'composer-actions';
            const decoy = document.createElement('input'); decoy.type = 'file'; decoy.hidden = true;
            document.querySelector('form').append(decoy);
            const input = document.createElement('input'); input.type = 'file'; input.hidden = true;
            document.querySelector('form').append(input);
            const action = document.createElement('button');
            action.setAttribute('aria-label', label);
            action.textContent = label;
            action.addEventListener('click', () => input.click());
            input.addEventListener('change', () => {
                window.uploads++;
                const card = document.createElement('div'); card.className = 'file-card';
                const file = document.createElement('button'); file.type = 'button'; file.textContent = name;
                const remove = document.createElement('button'); remove.type = 'button'; remove.textContent = '×';
                remove.setAttribute('aria-label', `${name} 제거`);
                const status = document.createElement('span'); status.textContent = `${name} 업로드 중`;
                card.append(file, remove, status); document.querySelector('form').append(card);
                window.finishUpload = () => {
                    status.remove(); document.querySelector('[data-testid=send-button]').disabled = false;
                };
                if (clearInputAfterChange) input.value = '';
            });
            menu.append(action); window.placeMenu(menu);
        });
    }""", {'name': path.name, 'label': action_name,
           'clearInputAfterChange': clear_input_after_change})
    clock = Clock()
    def sleep(seconds):
        clock.now += seconds
        if clock.now >= 1:
            page.evaluate('window.finishUpload && window.finishUpload()')
    clock.sleep = sleep
    monkeypatch.setattr(engine, 'time', clock)

    result = engine.attach_file(page, path)

    assert result == dict(state='confirmed', fallback_allowed=False,
                          reason='new_accessible_attachment_ready',
                          identity=path.name, filename=path.name)
    assert page.evaluate('window.uploads') == 1
    assert page.locator('input[type=file]').last.evaluate('e => e.files.length') == (
        0 if clear_input_after_change else 1)
    assert clock.now >= 2


@pytest.mark.parametrize(('markup', 'count'), [
    ('<button>Add Space files 파일을 둘러보고 검색하세요</button>', 0),
    ('<button>사진 및 파일 추가 컴퓨터에서 업로드</button>', 1),
    ('<button>사진 및 파일 추가 컴퓨터에서 업로드</button>'
     '<button>Upload from computer</button>', 2),
])
def test_current_upload_menu_action_requires_unique_accessible_match(
        engine, local_page, markup, count):
    local_page.set_content(markup)

    action, matches, reason = engine._current_file_menu_action(local_page)

    assert matches == count
    assert (action is not None) is (count == 1)
    assert reason == (None if count == 1 else
                      'upload_action_missing' if count == 0
                      else 'upload_action_ambiguous')


@pytest.mark.parametrize('composer_owns_menu', [True, False])
def test_current_attachment_requires_composer_owned_upload_menu(
        engine, local_page, tmp_path, monkeypatch, composer_owns_menu):
    """리뷰 F2: 이름이 같은 다른 업로더 버튼이 이미 열려 있어도, 이 composer의 "+"가 연 메뉴가 아니면
    파일을 전달하지 않는다. 이 composer가 이미 연 메뉴(aria-expanded=true)는 다시 누르지 않고 재사용한다."""
    page = local_page
    path = tmp_path / 'pack_open-action-review-run.md'
    path.write_text('synthetic pack')
    expanded = ' aria-expanded="true"' if composer_owns_menu else ''
    html = composer_html('current').replace('<button type="submit"',
                                             '<button disabled type="submit"')
    page.set_content(html.replace('</form>',
        '<input type="file" hidden><input type="file" hidden>'
        f'<button type="button" aria-label="파일 등 추가"{expanded}>+</button></form>'
        '<div class="composer-actions"><button type="button">'
        '사진 및 파일 추가 컴퓨터에서 업로드</button></div>'))
    page.evaluate(PLACE_MENU_JS)
    page.evaluate("() => window.placeMenu(document.querySelector('.composer-actions'))")
    page.evaluate("""name => {
        window.plusClicks = 0; window.actionClicks = 0; window.uploads = 0;
        const plus = document.querySelector('[aria-label="파일 등 추가"]');
        const input = document.querySelector('input[type=file]');
        plus.addEventListener('click', () => plusClicks++);  // 이 클릭은 메뉴를 열지 않는다(소유권 증거 없음)
        const action = document.querySelector('.composer-actions button');
        action.addEventListener('click', () => { actionClicks++; input.click(); });
        input.addEventListener('change', () => {
            uploads++;
            const card = document.createElement('div'); card.className = 'file-card';
            const file = document.createElement('button'); file.type = 'button'; file.textContent = name;
            const remove = document.createElement('button'); remove.type = 'button'; remove.textContent = '×';
            remove.setAttribute('aria-label', `${name} 제거`);
            const status = document.createElement('span'); status.textContent = `${name} 업로드 중`;
            card.append(file, remove, status); document.querySelector('form').append(card);
            window.finishUpload = () => {
                status.remove(); document.querySelector('[data-testid=send-button]').disabled = false;
            };
        });
    }""", path.name)
    clock = Clock()
    def sleep(seconds):
        clock.now += seconds
        if clock.now >= 1:
            page.evaluate('window.finishUpload && window.finishUpload()')
    clock.sleep = sleep
    monkeypatch.setattr(engine, 'time', clock)

    result = engine.attach_file(page, path)

    if composer_owns_menu:
        assert result == dict(state='confirmed', fallback_allowed=False,
                              reason='new_accessible_attachment_ready',
                              identity=path.name, filename=path.name)
        assert page.evaluate('window.plusClicks') == 0
        assert page.evaluate('window.actionClicks') == 1
    else:
        assert result['reason'] == 'upload_action_unowned'
        assert result['state'] != 'confirmed'
        assert page.evaluate('window.actionClicks') == 0
        assert page.evaluate('window.uploads') == 0


def test_current_accessible_attachment_rejects_existing_chip(engine, local_page, tmp_path):
    path = tmp_path / 'pack_new-review-run.md'
    path.write_text('synthetic pack')
    existing = accessible_attachment_card('prior-user-file.txt')
    local_page.set_content(composer_html('current').replace('</form>',
                         '<input type="file">' + existing + '</form>'))
    local_page.evaluate("""() => {
        window.uploads=0;
        document.querySelector('input[type=file]').addEventListener('change', () => uploads++);
    }""")

    result = engine.attach_file(local_page, path)

    assert result == dict(state='not_attempted', fallback_allowed=False,
                          reason='baseline_attachment_present')
    assert local_page.evaluate('window.uploads') == 0


def test_current_accessible_attachment_failure_reports_exact_cleanup_control(
        engine, local_page, tmp_path, monkeypatch, capsys):
    path = tmp_path / 'pack_unconfirmed-review-run.md'
    path.write_text('synthetic pack')
    local_page.set_content(composer_html('current').replace('</form>',
                         '<input type="file" hidden>'
                         '<button type="button" aria-label="파일 등 추가">+</button></form>'))
    local_page.evaluate(PLACE_MENU_JS)
    local_page.evaluate("""name => {
        const input = document.querySelector('input[type=file]');
        document.querySelector('[aria-label="파일 등 추가"]').addEventListener('click', e => {
            e.currentTarget.setAttribute('aria-expanded', 'true');  // 실측: 메뉴가 열리면 true
            const action = document.createElement('button');
            action.setAttribute('aria-label', '사진 및 파일 추가 컴퓨터에서 업로드');
            action.textContent = '사진 및 파일 추가 컴퓨터에서 업로드';
            action.addEventListener('click', () => input.click()); window.placeMenu(action);
        });
        input.addEventListener('change', () => {
            const card=document.createElement('div');card.className='file-card';
            const file=document.createElement('button');file.type='button';file.textContent=name;
            const remove=document.createElement('button');remove.type='button';remove.textContent='×';
            remove.setAttribute('aria-label', `${name} 제거`);
            const status=document.createElement('span');status.textContent=`${name} 업로드 중`;
            card.append(file, remove, status);document.querySelector('form').append(card);
        });
    }""", path.name)
    monkeypatch.setattr(engine, 'time', Clock())

    result = engine.attach_file(local_page, path)

    output = capsys.readouterr().out
    assert result['state'] == 'attempted_unconfirmed'
    assert result['reason'] == 'upload_readiness_unconfirmed'
    assert f"'{path.name} 제거'" in output


@pytest.mark.parametrize('mutation', ['removed', 'duplicate', 'uploading'])
def test_final_dispatch_revalidates_attachment_inside_atomic_guard(
        engine, local_page, monkeypatch, fixture_adapters, mutation):
    page = local_page
    name = 'pack_bound-review-run.md'
    card = accessible_attachment_card(name)
    page.set_content(composer_html('current').replace('</form>',
                      card + '<div id="overlay" style="position:fixed;inset:0;z-index:999;background:white"></div></form>'))
    event_counters(page)
    editor = engine.put_text(page, 'expected')
    page.evaluate("""({name, mutation}) => setTimeout(() => {
        const card=document.querySelector('.file-card');
        if(mutation==='removed') card.remove();
        if(mutation==='duplicate') {
            const extra=document.createElement('button');extra.textContent=name;card.append(extra);
        }
        if(mutation==='uploading') {
            const status=document.createElement('span');status.textContent=`${name} 업로드 중`;card.append(status);
        }
        document.querySelector('#overlay').style.display='none';
    }, 200)""", dict(name=name, mutation=mutation))
    dispatch = {}

    with pytest.raises(RuntimeError, match='첨부/대상 불일치'):
        engine.click_send(page, 'expected', editor, dispatch,
                          dict(identity=name, filename=name))

    assert dispatch == dict(state='NOT_DISPATCHED', reason='guard')
    assert page.evaluate('events') == dict(click=0, enter=0, submit=0)


def test_final_dispatch_accepts_same_ready_attachment_once(engine, local_page,
                                                            monkeypatch, fixture_adapters):
    page = local_page
    name = 'pack_bound-review-run.md'
    page.set_content(composer_html('current').replace('</form>',
                      accessible_attachment_card(name) + '</form>'))
    event_counters(page)
    editor = engine.put_text(page, 'expected')

    assert engine.click_send(page, 'expected', editor,
                             attachment=dict(identity=name, filename=name))
    assert page.evaluate('events') == dict(click=1, enter=0, submit=1)


@pytest.mark.parametrize('scenario', ['old_only', 'ordinary_text', 'delayed', 'title', 'aria-label', 'loading', 'progress_finishes', 'same_old_name'])
def test_new_attachment_identity_and_readiness(engine, local_page, tmp_path, monkeypatch, fixture_adapters, scenario):
    page = local_page
    path = tmp_path / 'pack_gptaku-plugins-codex_NEW.md'
    path.write_text('synthetic')
    old = path.name if scenario == 'same_old_name' else 'pack_gptaku-plugins-codex_OLD.md'
    page.set_content(composer_html('current') .replace('</form>', f'<input type="file"><span data-attachment-id="old" data-ready="true">{old}</span></form>'))
    page.evaluate("""({name, scenario}) => {
        window.addChip=() => {
            if(document.querySelector('[data-attachment-id=new]')) return;
            const chip=document.createElement('span'); chip.dataset.attachmentId='new';chip.dataset.ready='true';
            chip.innerText=(scenario==='title'||scenario==='aria-label')?'pack_gptaku…':name;
            if(scenario==='title'||scenario==='aria-label') chip.setAttribute(scenario,name);
            if(scenario==='loading'||scenario==='progress_finishes') chip.innerHTML+='<i role="progressbar">uploading</i>';
            document.querySelector('form').append(chip);
        };
        document.querySelector('input').addEventListener('change', () => {
            if(scenario==='ordinary_text') {
                const span=document.createElement('span');span.innerText=name;document.querySelector('form').append(span);
            } else if(!['old_only','same_old_name','delayed'].includes(scenario)) addChip();
        });
    }""", dict(name=path.name, scenario=scenario))
    clock = Clock()
    def sleep(seconds):
        clock.now += seconds
        if clock.now >= 2:
            if scenario == 'delayed':
                page.evaluate('addChip()')
            if scenario == 'progress_finishes':
                page.eval_on_selector('[role=progressbar]', "e => e.style.display='none'")
    clock.sleep = sleep
    monkeypatch.setattr(engine, 'time', clock)
    result = engine.attach_file(page, path)
    success = scenario in ['delayed', 'title', 'aria-label', 'progress_finishes']
    assert result['state'] == ('confirmed' if success else 'attempted_unconfirmed')
    assert result['fallback_allowed'] is False
    if scenario in ['delayed', 'progress_finishes']:
        assert clock.now >= 2


def run_binding(page):
    return dict(schema_version=2, run_id='checkpoint-test', chat_url=page.url,
        original_run_bound=True, baseline_user_ids=[], baseline_assistant_ids=[],
        sent_user_ids=[], assistant_ids=[], phase='SEND_PENDING',
        sent_text_sha256=hashlib.sha256(b'question').hexdigest())


def test_user_checkpoint_survives_assistant_ambiguity(engine, tmp_path):
    page = Page(current_html().replace('</div><button type="button" aria-label="복사">',
        '</div><div data-chatgpt-search-unit-key="t:3:assistant" data-chatgpt-search-message-ids="a2">second</div><button type="button" aria-label="복사">'))
    binding = run_binding(page)
    path = tmp_path / 'manifest.json'
    engine.persist_binding(path, binding)
    with pytest.raises(RuntimeError, match='유일하지'):
        engine.bound_reply(page, binding, lambda candidate: engine.persist_binding(path, candidate))
    saved = engine.validate_manifest_file(path)
    assert saved['phase'] == 'USER_BOUND' and saved['sent_user_ids'] == ['u1'] and saved['assistant_ids'] == []
    assert str(path) in engine.recovery_hint(binding, path)


def test_checkpoint_write_failure_never_suggests_manifest(engine, tmp_path):
    page = Page(current_html())
    binding = run_binding(page)
    path = tmp_path / 'manifest.json'
    engine.persist_binding(path, binding)
    def fail(candidate):
        raise OSError('checkpoint failed')
    with pytest.raises(RuntimeError, match='저장 실패'):
        engine.bound_reply(page, binding, fail)
    assert json.loads(path.read_text())['sent_user_ids'] == []
    hint = engine.recovery_hint(binding, path)
    assert str(path) not in hint and '수동 latest-user' in hint


def test_actual_cli_user_bound_parser_enters_assistant_wait(engine, tmp_path, monkeypatch):
    page = Page(current_html(answer=False))
    page.goto = lambda *a, **k: None
    page.close = lambda: None
    binding = run_binding(page)
    path = tmp_path / 'checkpoint.json'
    engine.bound_reply(page, binding, lambda candidate: engine.persist_binding(path, candidate))
    assert engine.validate_manifest_file(path)['assistant_ids'] == []
    class PW:
        def __enter__(self): return self
        def __exit__(self, *a): pass
    monkeypatch.setattr(engine, 'sync_playwright', PW)
    monkeypatch.setattr(engine, 'ensure_browser', lambda a: True)
    monkeypatch.setattr(engine, 'resolve_browser', lambda a: ('fixture', '/fixture'))
    monkeypatch.setattr(engine, 'connect_cdp', lambda p: object())
    monkeypatch.setattr(engine, 'pick_context', lambda b: SimpleNamespace(new_page=lambda: page))
    monkeypatch.setattr(engine, '_guard_dialogs', lambda *a: None)
    monkeypatch.setattr(engine, 'hide_browser_if_background', lambda: None)
    monkeypatch.setattr(engine, 'login_state', lambda p: 'ok')
    monkeypatch.setattr(engine, 'time', Clock())
    calls = []
    real = engine.response_snapshot
    def snapshot(p, b, **kw):
        calls.append(b['sent_user_ids'])
        return real(p, b, **kw)
    monkeypatch.setattr(engine, 'response_snapshot', snapshot)
    monkeypatch.setattr(sys, 'argv', ['pack_and_ask.py', '--harvest', str(path), '--out-dir', str(tmp_path), '--max-wait', '1', '--retries', '0'])
    with pytest.raises(SystemExit, match='응답 회수 실패'):
        engine.main()
    assert calls and all(ids == ['u1'] for ids in calls)
    assert not list(tmp_path.glob('response_*.md'))


def test_failed_assistant_checkpoint_forces_manual_even_with_valid_disk(engine, tmp_path):
    page = Page(current_html())
    binding = run_binding(page)
    path = tmp_path / 'checkpoint.json'
    def persist(candidate):
        if candidate['phase'] == 'ASSISTANT_BOUND':
            raise OSError('synthetic write failure')
        engine.persist_binding(path, candidate)
    with pytest.raises(RuntimeError, match='저장 실패'):
        engine.bound_reply(page, binding, persist)
    assert engine.validate_manifest_file(path)['phase'] == 'USER_BOUND'
    hint = engine.recovery_hint(binding, path)
    assert str(path) not in hint and '수동 latest-user' in hint


@pytest.mark.parametrize('state,allowed', [('attempted_unconfirmed', False), ('not_attempted', False), ('not_attempted', True)])
def test_cli_attachment_fallback_policy(engine, tmp_path, monkeypatch, capsys, state, allowed):
    page = Page(current_html())
    page.goto = lambda *a, **k: None
    page.close = lambda: None
    pack = tmp_path / 'synthetic.md'
    pack.write_text('synthetic pack')
    class PW:
        def __enter__(self): return self
        def __exit__(self, *a): pass
    for name, value in dict(sync_playwright=PW, ensure_browser=lambda a: True,
            resolve_browser=lambda a: ('fixture', '/fixture'), connect_cdp=lambda p: object(),
            pick_context=lambda b: SimpleNamespace(new_page=lambda: page),
                _guard_dialogs=lambda *a: None, hide_browser_if_background=lambda: None,
                login_state=lambda p: 'ok', ensure_chat_mode=lambda p: (True, 'chat'),
                pack_repo=lambda *a, **kw: (pack, 1), put_text=lambda *a: None,
                composer_has_prompt=lambda *a: True,
                attach_file=lambda *a: dict(state=state, fallback_allowed=allowed,
                                            reason='file_input_missing_or_ambiguous')).items():
        monkeypatch.setattr(engine, name, value)
    monkeypatch.setattr(engine, 'time', Clock())
    fallback, dispatch = [], []
    monkeypatch.setattr(engine, 'build_paste_fallback', lambda *a: fallback.append(1) or 'inline synthetic pack')
    def click(*a):
        dispatch.append(1)
        raise OSError('synthetic stop after dispatch')
    monkeypatch.setattr(engine, 'click_send', click)
    monkeypatch.setattr(sys, 'argv', ['pack_and_ask.py', '--target', str(tmp_path), '--no-project',
                        '--prompt', 'question', '--out-dir', str(tmp_path), '--retries', '0'])
    with pytest.raises(SystemExit):
        engine.main()
    assert len(fallback) == len(dispatch) == (1 if allowed else 0)
    diagnostic = capsys.readouterr().out
    assert f"state={state}" in diagnostic
    assert "reason=file_input_missing_or_ambiguous" in diagnostic


def offline_main(engine, page, tmp_path, monkeypatch, pack=None):
    """Keep real parser/handler/input/attachment/dispatch; replace transport only."""
    class PW:
        def __enter__(self): return self
        def __exit__(self, *a): pass
    monkeypatch.setattr(page, 'goto', lambda *a, **kw: None)
    monkeypatch.setattr(page, 'close', lambda: None)
    for name, value in dict(sync_playwright=PW, ensure_browser=lambda a: True,
            resolve_browser=lambda a: ('fixture', '/fixture'), connect_cdp=lambda p: object(),
            pick_context=lambda b: SimpleNamespace(new_page=lambda: page),
            _guard_dialogs=lambda *a: None, hide_browser_if_background=lambda: None,
            login_state=lambda p: 'ok', ensure_chat_mode=lambda p: (True, 'chat')).items():
        monkeypatch.setattr(engine, name, value)
    monkeypatch.setattr(engine, 'time', Clock())
    args = ['pack_and_ask.py', '--no-project', '--prompt', 'question',
            '--out-dir', str(tmp_path), '--retries', '3']
    if pack:
        monkeypatch.setattr(engine, 'pack_repo', lambda *a, **kw: (pack, 1))
        args += ['--target', str(tmp_path)]
    monkeypatch.setattr(sys, 'argv', args)


def offline_preflight_main(engine, page, tmp_path, monkeypatch, model):
    """Run the real CLI preflight and mode/model validators with only CDP transport stubbed."""
    class PW:
        def __enter__(self): return self
        def __exit__(self, *a): pass
    monkeypatch.setattr(page, 'goto', lambda *a, **kw: None)
    monkeypatch.setattr(page, 'close', lambda: None)
    for name, value in dict(sync_playwright=PW, ensure_browser=lambda a: True,
            resolve_browser=lambda a: ('fixture', '/fixture'), connect_cdp=lambda p: object(),
            pick_context=lambda b: SimpleNamespace(new_page=lambda: page),
            _guard_dialogs=lambda *a: None, hide_browser_if_background=lambda: None,
            login_state=lambda p: 'ok').items():
        monkeypatch.setattr(engine, name, value)
    monkeypatch.setattr(engine, 'time', Clock())
    monkeypatch.setattr(sys, 'argv', ['pack_and_ask.py', '--no-project', '--prompt', 'question',
        '--out-dir', str(tmp_path), '--retries', '0', '--model', model])


@pytest.mark.parametrize('scenario', ['read_exception', 'trigger_marker'])
def test_cli_model_preflight_diagnostics_hide_private_markers(engine, local_page, tmp_path,
                                                               monkeypatch, capsys, scenario):
    page = local_page
    marker = 'PRIVATE_PREFLIGHT_MARKER_9F1A'
    page.set_content(current_html() + mode_html(chat_pressed='true', work_pressed='false'))
    page.eval_on_selector('#trigger', '(el, text) => el.innerText=text', marker)
    offline_preflight_main(engine, page, tmp_path, monkeypatch, 'pro')
    if scenario == 'read_exception':
        evaluate = page.evaluate
        def fail_mode_read(script, *args, **kwargs):
            if script == engine.JS_READ_MODE:
                raise RuntimeError(marker)
            return evaluate(script, *args, **kwargs)
        monkeypatch.setattr(page, 'evaluate', fail_mode_read)
    sent = []
    monkeypatch.setattr(engine, 'click_send', lambda *a, **kw: sent.append('send'))
    monkeypatch.setattr(engine, 'attach_file', lambda *a, **kw: pytest.fail('unexpected upload'))
    with pytest.raises(SystemExit) as caught:
        engine.main()
    output = capsys.readouterr()
    assert marker not in output.out + output.err + str(caught.value)
    assert not sent
    assert set(engine._DISPATCH_ADAPTERS) == {'current'} and set(engine._ATTACHMENT_ADAPTERS) == {'current'}


def test_cli_unknown_mode_blocks_non_pro_model_before_selection(engine, local_page, tmp_path,
                                                                monkeypatch, capsys):
    page = local_page
    page.set_content(current_html() + mode_html(chat_pressed='false', work_pressed='false'))
    offline_preflight_main(engine, page, tmp_path, monkeypatch, 'high')
    selected = []
    monkeypatch.setattr(engine, 'select_model', lambda *a, **kw: selected.append('select'))
    sent = []
    monkeypatch.setattr(engine, 'click_send', lambda *a, **kw: sent.append('send'))
    monkeypatch.setattr(engine, 'attach_file', lambda *a, **kw: pytest.fail('unexpected upload'))
    with pytest.raises(SystemExit) as caught:
        engine.main()
    output = capsys.readouterr()
    assert not selected and not sent
    assert '모델/추론단계 사전검증 시작' not in output.out
    assert '전송 전 실패' in str(caught.value)
    assert set(engine._DISPATCH_ADAPTERS) == {'current'} and set(engine._ATTACHMENT_ADAPTERS) == {'current'}


@pytest.mark.parametrize('stage', ['baseline', 'after_assignment', 'multiple_owners'])
def test_cli_unassociated_progress_blocks_upload_and_send(engine, local_page, tmp_path,
                                                         monkeypatch, fixture_adapters, stage):
    page = local_page
    pack = tmp_path / 'requested-complete-filename.md'
    pack.write_text('synthetic source')
    chip = '<span data-attachment-id="old" data-ready="true">old.md</span>'
    progress = '<i role="progressbar">uploading</i>'
    if stage == 'baseline':
        chip += progress
    elif stage == 'multiple_owners':
        chip = ('<span data-attachment-id="old" data-ready="true">old.md'
                '<span data-attachment-id="other" data-ready="true">other.md'
                + progress + '</span></span>')
    page.set_content(composer_html('current').replace('</form>', '<input type="file">' + chip + '</form>'))
    event_counters(page)
    page.evaluate("""name => {
        window.uploads=0;
        document.querySelector('input').addEventListener('change', () => {
            uploads++;
            const chip=document.createElement('span');
            chip.dataset.attachmentId='fresh';chip.dataset.ready='true';chip.textContent=name;
            document.querySelector('form').append(chip);
            document.querySelector('form').insertAdjacentHTML('beforeend','<i role="progressbar">uploading</i>');
        });
    }""", pack.name)
    offline_main(engine, page, tmp_path, monkeypatch, pack)
    real_attach = engine.attach_file
    results, fallback = [], []
    def attach(*args):
        result = real_attach(*args)
        results.append(result)
        return result
    monkeypatch.setattr(engine, 'attach_file', attach)
    monkeypatch.setattr(engine, 'build_paste_fallback', lambda *a: fallback.append(1))
    with pytest.raises(SystemExit, match='전송 전 실패'):
        engine.main()
    assert len(results) == 1
    assert results[0]['state'] == ('attempted_unconfirmed' if stage == 'after_assignment' else 'not_attempted')
    assert results[0]['fallback_allowed'] is False and not fallback
    assert page.evaluate('uploads') == (1 if stage == 'after_assignment' else 0)
    assert page.evaluate('events') == dict(click=0, enter=0, submit=0)
    if stage == 'after_assignment':
        assert page.inner_text('[data-attachment-id=fresh]') == pack.name
        assert page.get_attribute('[data-attachment-id=fresh]', 'data-ready') == 'true'


@pytest.mark.parametrize('mode', ['click', 'enter'])
@pytest.mark.parametrize('failure', ['unsupported', 'guard_false', 'checkpoint', 'ack_loss', 'ack_invalid', 'preparation'])
def test_cli_dispatch_evidence_and_sanitized_diagnostics(engine, local_page, tmp_path,
        monkeypatch, fixture_adapters, capsys, mode, failure):
    page = local_page
    page.set_content(composer_html('current', button=mode == 'click'))
    event_counters(page)
    offline_main(engine, page, tmp_path, monkeypatch)
    secret = 'synthetic-private-error-marker'
    if failure == 'unsupported':
        monkeypatch.setattr(engine, '_DISPATCH_ADAPTERS', {})
    real_persist = engine.persist_binding
    checkpoints = []
    def persist(path, value):
        if value.get('phase') == 'SEND_PENDING':
            assert page.evaluate('events') == dict(click=0, enter=0, submit=0)
            if failure == 'checkpoint':
                raise OSError(secret)
            real_persist(path, value)
            checkpoints.append(path)
            if failure == 'guard_false':
                page.eval_on_selector('[contenteditable]', '(e, text) => e.innerText=text', secret)
        else:
            real_persist(path, value)
    monkeypatch.setattr(engine, 'persist_binding', persist)
    real_evaluate = page.evaluate
    def evaluate(script, *args, **kw):
        if 'sendSelector' in script:
            assert checkpoints and json.loads(checkpoints[0].read_text())['phase'] == 'SEND_PENDING'
            result = real_evaluate(script, *args, **kw)
            if failure == 'ack_loss':
                # Even a guard-like exception message is not authoritative rejection.
                raise RuntimeError('전송 직전 composer/본문/대상 불일치 ' + secret)
            if failure == 'ack_invalid':
                return None  # Missing acknowledgement is not an authoritative false.
            return result
        return real_evaluate(script, *args, **kw)
    monkeypatch.setattr(page, 'evaluate', evaluate)
    if failure == 'preparation':
        real_query = page.query_selector_all
        def query(selector):
            if 'composer-send-button' in selector:
                raise OSError(secret)
            return real_query(selector)
        monkeypatch.setattr(page, 'query_selector_all', query)
    with pytest.raises(SystemExit) as caught:
        engine.main()
    final = str(caught.value)
    output = capsys.readouterr()
    assert secret not in output.out + output.err + final
    assert '실행 단계 실패' in output.err
    assert '방금 생긴 채팅' not in final
    events = real_evaluate('events')
    if failure in ('ack_loss', 'ack_invalid'):
        assert events == dict(click=int(mode == 'click'), enter=int(mode == 'enter'), submit=1)
        assert '전송 시도 결과/대화 위치 미확인' in final
        assert '자동 재전송하지 않습니다' in final and '대화가 있을 때만' in final
        assert '활성화 결과 미확정' in output.err
    else:
        assert events == dict(click=0, enter=0, submit=0)
        assert '전송 전 실패' in final and '이 실행은 전송되지 않았습니다' in final
        assert '--harvest' not in final and '대화 위치 미확인' not in final
        reason = dict(unsupported='adapter unsupported', guard_false='대상 불일치',
                      checkpoint='checkpoint 저장 실패', preparation='준비/검증 실패')[failure]
        assert reason in final and reason in output.err
    for path in checkpoints:
        # A durable intent is not a harvestable user binding.
        with pytest.raises(ValueError):
            engine.validate_manifest_file(path)


# ---- 2026-10-01 실측 회귀: 접힌 user 본문 / 코드 블록 복사 버튼 / 어시스턴트 라벨 / 제거 라벨 ----
LONG_PROMPT = ('`insane-review-codex` 전체를 리뷰해 주세요. `INSANE_REVIEW_CDP_PORT=9225`만 지정한 `--check-env`는 '
               'login/cookie unknown을 반환했습니다.\n\n(참고: 이번 메시지에 첨부된 파일만 근거로 답하라.)')


def run_binding_for(engine, text, with_fingerprint=True):
    binding = dict(schema_version=2, run_id='collapse-test', original_run_bound=True,
                   baseline_user_ids=[], baseline_assistant_ids=[], sent_user_ids=[], assistant_ids=[],
                   phase='SEND_PENDING',
                   sent_text_sha256=hashlib.sha256(engine_normalize(text).encode()).hexdigest())
    if with_fingerprint:
        binding['sent_text_fingerprint'] = engine.message_fingerprint(text)
    return binding


def engine_normalize(text):
    return __import__('re').sub(r'\s+', ' ', text).strip()


def user_page(shown):
    return Page(current_html().replace('>question</div>', f'>{shown}</div>'))


# ChatGPT가 실제로 보여 준 형태: 인라인 코드 백틱이 사라지고 말미에 접힘 표시가 붙는다.
RENDERED = LONG_PROMPT.replace('`', '') + ' … 더 보기'


def test_user_body_binds_when_chatgpt_collapses_and_renders_markdown(engine):
    page = user_page(RENDERED)
    binding = run_binding_for(engine, LONG_PROMPT)
    node = engine.bound_reply(page, binding)
    assert binding['sent_user_ids'] == ['u1']
    assert node is not None


def test_user_body_exact_match_still_binds_without_fingerprint(engine):
    page = user_page(LONG_PROMPT)
    binding = run_binding_for(engine, LONG_PROMPT, with_fingerprint=False)
    assert engine.bound_reply(page, binding) is not None


@pytest.mark.parametrize('shown,with_fingerprint', [
    (RENDERED, False),                       # 지문 없는 구 manifest: 접힌 표시는 여전히 거부
    ('다른 질문입니다 … 더 보기', True),        # 다른 본문은 지문이 달라 거부
])
def test_user_body_mismatch_is_still_rejected(engine, shown, with_fingerprint):
    page = user_page(shown)
    with pytest.raises(RuntimeError, match='전송 user 본문 불일치'):
        engine.bound_reply(page, run_binding_for(engine, LONG_PROMPT, with_fingerprint=with_fingerprint))


@pytest.mark.parametrize('sent,shown', [
    ('검토: x > 0 이면 승인', '검토: x < 0 이면 승인'),            # 리뷰 F2: 문자 골격은 같고 의미만 다름
    ('검토: a == b 이면 승인', '검토: a != b 이면 승인'),
    ('한도는 3.5 입니다', '한도는 35 입니다'),                      # 숫자 토큰이 다름
    ('한도는 1,000 입니다', '한도는 1000 입니다'),
])
def test_user_body_fingerprint_rejects_symbol_only_meaning_changes(engine, sent, shown):
    page = user_page(shown)
    with pytest.raises(RuntimeError, match='전송 user 본문 불일치'):
        engine.bound_reply(page, run_binding_for(engine, sent))


@pytest.mark.parametrize('shown', [
    '**검토**: `x > 0` 이면 승인 … 더 보기',     # 굵게·인라인 코드·접힘 표시
    '검토:   x > 0\n\n이면 승인',                # 공백/줄바꿈 차이
])
def test_user_body_fingerprint_accepts_markdown_rendering_differences(engine, shown):
    page = user_page(shown)
    binding = run_binding_for(engine, '검토: x > 0 이면 승인')
    assert engine.bound_reply(page, binding) is not None


def code_block_answer(inside_buttons, outside_buttons):
    header = '<div role="presentation"><button type="button">자동 줄 바꿈 사용</button><button type="button" aria-label="복사"></button></div>'
    html = current_html()
    html = html.replace('>answer</div><button type="button" aria-label="복사"></button>',
                        '>answer' + header * inside_buttons + '</div>' + '<button type="button" aria-label="복사"></button>' * outside_buttons)
    return Page(html)


@pytest.mark.parametrize('inside,outside,complete', [
    (5, 1, True),    # 실측: 코드 블록 헤더 복사 5개(노드 안) + 턴 툴바 복사 1개(노드 밖)
    (0, 1, True),    # 코드 블록 없는 짧은 응답
    (5, 2, False),   # 노드 밖 복사 버튼이 둘 이상이면 모호 → 미완
    (2, 0, False),   # 노드 밖 툴바가 없고 안에만 여럿이면 모호 → 미완
    (1, 0, False),   # 리뷰 F3: 코드 헤더 버튼 하나뿐이면 완료 증거가 아니다
    (0, 0, False),
])
def test_turn_copy_button_ignores_code_block_headers_inside_answer(engine, inside, outside, complete):
    page = code_block_answer(inside, outside)
    node = engine.message_nodes(page, 'assistant')[0]
    assert (engine.node_copy_button(node) is not None) is complete
    assert (engine.response_snapshot(page, manual_binding_for(page)) is not None) is complete


def manual_binding_for(page):
    return {'chat_url': page.url, 'original_run_bound': False,
            'harvest_mode': 'manual_latest_user', 'phase': 'MANUAL_SELECT'}


def test_response_text_drops_screen_reader_label(engine):
    page = Page(current_html().replace('>answer</div>', '>ChatGPT 답변:\n\n결론: REVISE</div>'))
    snapshot = engine.response_snapshot(page, manual_binding_for(page))
    assert snapshot == (('a1',), '결론: REVISE')


def test_failure_detail_shows_only_known_reasons(engine):
    assert engine.failure_detail(RuntimeError('전송 user 본문 불일치')) == '전송 user 본문 불일치'
    assert engine.failure_detail(RuntimeError('전송 user 본문 불일치 synthetic-secret')) == 'RuntimeError'
    assert engine.failure_detail(OSError('synthetic-secret')) == 'OSError'
    assert engine.failure_detail(RuntimeError('a', 'b')) == 'RuntimeError'


@pytest.mark.parametrize('label', ['pack_x.md 제거', 'pack_x.md remove', 'Remove pack_x.md', '제거 pack_x.md'])
def test_remove_label_rule_is_shared_by_attach_and_final_guard(engine, local_page, label):
    """리뷰 F5: 첨부 확인이 받아들이는 제거 라벨은 최종 전송 가드도 받아들여야 한다."""
    name = 'pack_x.md'
    assert engine._REMOVE_CONTROL_RE.match(label)
    page = local_page
    card = (f'<div class="file-card"><button type="button">{name}</button>'
            f'<button type="button" aria-label="{label}">×</button></div>')
    page.set_content(composer_html('current').replace('</form>', card + '</form>'))
    event_counters(page)
    editor = engine.put_text(page, 'expected')
    assert engine._attachment_remove_locator(page.locator('form'), name).count() == 1
    dispatch = {}
    assert engine.click_send(page, 'expected', editor, dispatch, dict(identity=name, filename=name))
    assert dispatch['state'] == 'ACTIVATED'
    assert page.evaluate('events')['submit'] == 1


@pytest.mark.parametrize('reverse,position,at_tail', [
    (True, 'bottom', True),     # 실측: column-reverse 스레드는 scrollTop=0이 맨 아래
    (True, 'top', False),       # 위로 올리면 scrollTop이 음수
    (False, 'bottom', True),    # 일반 스크롤러의 기존 동작 유지
    (False, 'top', False),
])
def test_tail_confirmed_understands_column_reverse_thread(engine, local_page, reverse, position, at_tail):
    direction = 'column-reverse' if reverse else 'column'
    local_page.set_content(
        f'<div id="s" style="display:flex;flex-direction:{direction};overflow-y:auto;height:200px">'
        '<div style="height:3000px;flex-shrink:0"><div data-chatgpt-search-unit-key="t:0:user" '
        'data-chatgpt-search-message-ids="u1">question</div></div></div>')
    local_page.evaluate("""([reverse, position]) => {
        const s = document.getElementById('s');
        if (position === 'bottom') s.scrollTop = reverse ? 0 : s.scrollHeight;
        else s.scrollTop = reverse ? -(s.scrollHeight - s.clientHeight) : 0;
    }""", [reverse, position])
    assert engine.tail_confirmed(local_page) is at_tail


CONV = '11111111-2222-4333-8444-555555555555'
BOUND = f'https://chatgpt.com/g/g-p-0123456789abcdef0123456789abcdef-example-project-12345678/c/{CONV}'


@pytest.mark.parametrize('current,same', [
    (BOUND, True),
    (f'https://chatgpt.com/g/g-p-0123456789abcdef0123456789abcdef/c/{CONV}', True),   # 실측: SPA가 슬러그를 잠깐 뗌
    (f'https://chatgpt.com/c/{CONV}?model=x', True),
    (BOUND.replace('11111111', '11111112'), False),                                  # 다른 대화
    (f'https://evil.example/c/{CONV}', False),                                        # 다른 origin
    ('https://chatgpt.com/g/g-p-0123456789abcdef0123456789abcdef/project', False),     # 대화 아님
])
def test_response_snapshot_binds_by_conversation_id_not_url_string(engine, current, same):
    page = Page(current_html())
    page.url = current
    binding = manual_binding_for(page)
    binding['chat_url'] = BOUND
    if same:
        assert engine.response_snapshot(page, binding) is not None
    else:
        with pytest.raises(RuntimeError, match='결속 대화 이탈'):
            engine.response_snapshot(page, binding)


@pytest.mark.parametrize('progress', [
    'uploading… {name}', 'uploading {name}', '{name} uploading…', '{name} 업로드 중', 'uploading... {name}',
])
def test_final_guard_uses_the_same_progress_rule_as_attachment_check(engine, local_page, progress):
    """리뷰 F4: 첨부 확인이 진행 중으로 보는 표기를 최종 전송 가드도 진행 중으로 봐야 한다."""
    name = 'pack_x.md'
    page = local_page
    page.set_content(composer_html('current').replace('</form>',
        accessible_attachment_card(name) + f'<span>{progress.format(name=name)}</span></form>'))
    event_counters(page)
    editor = engine.put_text(page, 'expected')
    assert engine._visible_locator_count(engine._attachment_progress_locator(page.locator('form'), name)) == 1
    dispatch = {}
    with pytest.raises(RuntimeError, match='첨부/대상 불일치'):
        engine.click_send(page, 'expected', editor, dispatch, dict(identity=name, filename=name))
    assert dispatch == dict(state='NOT_DISPATCHED', reason='guard')
    assert page.evaluate('events')['submit'] == 0


def test_final_guard_blocks_aria_label_progressbar(engine, local_page):
    """실측: 업로드 중에는 role=progressbar의 aria-label이 '<파일명> 업로드 중'이다."""
    name = 'pack_x.md'
    page = local_page
    page.set_content(composer_html('current').replace('</form>',
        accessible_attachment_card(name) + f'<div role="progressbar" aria-label="{name} 업로드 중"></div></form>'))
    event_counters(page)
    editor = engine.put_text(page, 'expected')
    with pytest.raises(RuntimeError, match='첨부/대상 불일치'):
        engine.click_send(page, 'expected', editor, {}, dict(identity=name, filename=name))
    assert page.evaluate('events')['submit'] == 0


def test_expanded_composer_menu_does_not_authorize_a_distant_uploader(engine, local_page, tmp_path, monkeypatch):
    """리뷰 F1: 이 composer의 메뉴가 열려 있어도(aria-expanded=true), 클릭할 항목이 그 메뉴 소속이 아니면
    (다른 곳의 업로더) 파일을 전달하지 않는다."""
    page = local_page
    path = tmp_path / 'pack_distant-review-run.md'
    path.write_text('synthetic pack')
    html = composer_html('current').replace('<button type="submit"', '<button disabled type="submit"')
    page.set_content(html.replace('</form>',
        '<button type="button" aria-label="파일 등 추가" aria-expanded="true">+</button></form>'
        '<div id="other" style="position:absolute;left:600px;top:300px">'
        '<button type="button">Upload from computer</button><input type="file" hidden>'
        '<button type="button">Other action</button></div>'))
    page.evaluate("""() => {
        window.uploads = 0; window.actionClicks = 0;
        const input = document.querySelector('#other input');
        document.querySelector('#other button').addEventListener('click', () => { actionClicks++; input.click(); });
        input.addEventListener('change', () => uploads++);
    }""")
    monkeypatch.setattr(engine, 'time', Clock())

    result = engine.attach_file(page, path)

    assert result['reason'] == 'upload_action_unowned'
    assert result['state'] != 'confirmed'
    assert page.evaluate('window.actionClicks') == 0
    assert page.evaluate('window.uploads') == 0


# ---- 리뷰 F6/F4(N5)/F1(O1)/F3(O3) 회귀 ----
def test_force_answer_requires_bound_user_to_be_the_latest_turn(engine):
    page = Page(current_html())
    binding = dict(sent_user_ids=['u1'])
    assert engine.bound_user_is_latest(page, binding) is True
    page.soup.select_one('[data-turn-key]').append(__import__('bs4').BeautifulSoup(
        '<div data-chatgpt-search-unit-key="t:3:user" data-content-search-unit-key="t:3:user" '
        'data-chatgpt-search-message-ids="u2">later</div>', 'html.parser'))
    assert engine.bound_user_is_latest(page, binding) is False     # 다른(나중) user 턴이 생성 중일 수 있음
    assert engine.bound_user_is_latest(None, binding) is False       # 확인 실패는 누르지 않는다


def test_force_answer_is_not_clicked_for_another_turn(engine, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(engine, 'time', clock)
    monkeypatch.setattr(engine, 'MIN_WAIT_SECS', 0)
    monkeypatch.setattr(engine, 'detect_quota_block', lambda p: None)
    monkeypatch.setattr(engine, 'error_surface_state', lambda p: 'clear')
    monkeypatch.setattr(engine, 'streaming_state', lambda p: 'streaming')
    monkeypatch.setattr(engine, 'response_snapshot', lambda p, b, **kw: None)
    monkeypatch.setattr(engine, 'bound_user_is_latest', lambda p, b: False)
    clicks = []
    monkeypatch.setattr(engine, 'click_answer_now', lambda p: clicks.append(1) or True)
    binding = {'chat_url': 'url', 'sent_user_ids': ['u1'], 'phase': 'USER_BOUND'}
    engine.wait_for_turn_response(None, max_wait=3, force_after=1, binding=binding)
    assert clicks == [] and 'forced_answer' not in binding


@pytest.mark.parametrize('url,ok', [
    ('https://chatgpt.com/g/g-p-0123456789abcdef0123456789abcdef-example-project-12345678/project', True),
    ('https://chatgpt.com/g/g-p-0123456789abcdef0123456789abcdef/project', True),
    ('https://chatgpt.com/g/g-p-0123456789abcdef0123456789abcdef', True),
    ('https://untrusted.invalid/g/g-p-0123456789abcdef0123456789abcdef/project', False),   # 리뷰 F1: 외부 origin
    ('http://chatgpt.com/g/g-p-0123456789abcdef0123456789abcdef/project', False),
    ('https://chatgpt.com.evil.example/g/g-p-0123456789abcdef0123456789abcdef/project', False),
    ('https://user:pw@chatgpt.com/g/g-p-0123456789abcdef0123456789abcdef/project', False),
    ('https://chatgpt.com:8443/g/g-p-0123456789abcdef0123456789abcdef/project', False),
    ('https://chatgpt.com/c/11111111-2222-4333-8444-555555555555', False),
    (None, False),
])
def test_project_url_requires_chatgpt_origin_and_project_path(engine, url, ok):
    assert engine.project_url_ok(url) is ok


def test_external_project_url_is_dead_without_navigation(engine):
    class NoGoto:
        def goto(self, *args, **kwargs):
            raise AssertionError('외부 origin으로 이동하면 안 된다')
    url = 'https://untrusted.invalid/g/g-p-0123456789abcdef0123456789abcdef/project'
    assert engine.project_home_state(NoGoto(), url) == engine.PROJECT_DEAD


@pytest.mark.parametrize('state,creates', [
    ('unknown', False), ('auth', False),     # 리뷰 F4: 존재가 확인된 프로젝트의 불확실 상태는 '없음'이 아니다
    ('dead', True),                          # 없음이 확정된 경우에만 생성
])
def test_discovered_project_state_unknown_never_creates_a_new_project(engine, tmp_path, monkeypatch, state, creates):
    found = 'https://chatgpt.com/g/g-p-0123456789abcdef0123456789abcdef-x/project'
    created = []
    monkeypatch.setattr(engine, 'current_workspace_id', lambda page: None)
    monkeypatch.setattr(engine, '_open_chat_home', lambda page: True)
    monkeypatch.setattr(engine, 'find_project_url_api', lambda page, name: found)
    monkeypatch.setattr(engine, 'find_project_url', lambda page, name: None)
    monkeypatch.setattr(engine, 'project_home_state', lambda page, url: state if url == found else engine.PROJECT_OK)
    monkeypatch.setattr(engine, 'create_project',
                        lambda page, name: created.append(name) or 'https://chatgpt.com/g/g-p-' + 'b' * 32 + '/project')
    result = engine._ensure_project_locked(object(), 'proj', 'key', tmp_path / 'projects.json')
    assert bool(created) is creates
    assert (result is not None) is creates
    cache_file = tmp_path / 'projects.json'
    if creates:
        assert 'b' * 32 in cache_file.read_text()     # 대체물이 검증된 뒤에만 캐시에 기록
    else:
        assert not cache_file.exists()                  # 불확실 상태에서는 캐시를 건드리지 않는다


def test_browser_name_and_absolute_path_resolve_to_the_same_identity(engine, tmp_path, monkeypatch):
    real = tmp_path / 'Google Chrome'
    real.write_text('#!/bin/sh')
    link = tmp_path / 'alias-to-chrome'
    link.symlink_to(real)
    monkeypatch.setattr(engine, 'detect_browsers', lambda: [('Chrome', str(real)), ('Brave', str(tmp_path / 'Brave'))])
    assert engine.resolve_browser('Chrome')[0] == 'Chrome'
    assert engine.resolve_browser(str(real))[0] == 'Chrome'           # 리뷰 F3: 파일명(Google Chrome)이 아니라 등록 이름
    assert engine.resolve_browser(str(link))[0] == 'Chrome'           # 심볼릭 링크도 같은 실행파일
    other = tmp_path / 'Other'
    other.write_text('#!/bin/sh')
    assert engine.resolve_browser(str(other))[0] == 'Other'           # 미등록 실행파일은 기존처럼 stem


def test_profile_owner_stored_as_absolute_path_matches_registered_name(engine, tmp_path, monkeypatch):
    real = tmp_path / 'Google Chrome'
    real.write_text('#!/bin/sh')
    monkeypatch.setattr(engine, 'detect_browsers', lambda: [('Chrome', str(real))])
    monkeypatch.setattr(engine, '_load_config', lambda: {'profile_owner': str(real), 'browser': 'Chrome'})
    assert engine.profile_dir_for('Chrome', persist_owner=False) == engine.BROWSER_PROFILE_DIR
    assert engine.profile_dir_for('Brave', persist_owner=False) != engine.BROWSER_PROFILE_DIR
