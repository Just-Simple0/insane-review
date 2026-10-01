#!/usr/bin/env python3
"""
insane-review — repomix 패킹 → 구독 ChatGPT(웹) GPT Pro(최신 플래그십) 투입 → 분석 회수 (API 비용 0)

흐름:
  1) 분석 대상 폴더를 repomix로 단일 파일 패킹 (--compress, secretlint 기본 on)
  2) Comet/Chrome를 CDP로 attach → 로그인된 chatgpt.com 세션 재사용
  3) 패킹본을 '파일 첨부' + 짧은 프롬프트로 투입 (모델/추론단계 검증)
  4) 턴 단위로 응답 완료를 판정(stop-button 사라짐 + copy 버튼 등장 + 텍스트 안정) → 회수
  5) 응답을 .md로 원자적 저장

v2 (2026-06-20): GPT-5.5 Pro 리뷰 반영 — 턴-스코프 판정, 모델 검증, fail-closed CDP/로그인,
force-answer 재시도, UUID/PID 파일명, repomix 버전 핀+timeout, 권한/시크릿, env 설정화.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager, ExitStack
from contextvars import ContextVar
from functools import wraps
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.parse
import uuid
from datetime import datetime
from pathlib import Path

# ---- 선택 의존성(라이브 모드에서만 필요) ----
try:
    import pyperclip
except ImportError:
    pyperclip = None
try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
except ImportError:
    sync_playwright = None
    class PlaywrightTimeoutError(Exception):
        pass

# ---------------------------------------------------------------------------
# 설정 (env로 오버라이드 가능 — 하드코딩 탈피)
# ---------------------------------------------------------------------------
COMET_PATH = os.environ.get("INSANE_REVIEW_COMET", "/Applications/Comet.app/Contents/MacOS/Comet")
CHROME_PATH = os.environ.get("INSANE_REVIEW_CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
CDP_PORT = int(os.environ.get("INSANE_REVIEW_CDP_PORT", "9222"))
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"
# 전용(격리) 프로필 — 사용자 주 브라우저 세션과 분리. Chrome 136+는 '기본 프로필'에서
# --remote-debugging-port를 정책적으로 무시하므로(쿠키 탈취 방지), 이 별도 user-data-dir이
# 없으면 디버그 포트가 아예 안 열린다. 모든 OS 공통으로 전용 프로필을 쓴다.
BROWSER_PROFILE_DIR = Path(os.environ.get(
    "INSANE_REVIEW_PROFILE", str(Path.home() / ".insane-review" / "browser-profile")))
# 선택한 브라우저를 영속화(재질문 방지) — 우선순위: --browser > env > config 저장값 > 첫 감지.
CONFIG_PATH = Path(os.environ.get(
    "INSANE_REVIEW_CONFIG", str(Path.home() / ".insane-review" / "config.json")))
# repomix 버전 핀(재현성·공급망) — env로 갱신. 빈 문자열이면 latest.
REPOMIX_VERSION = os.environ.get("INSANE_REVIEW_REPOMIX_VERSION", "1.15.0")
REPOMIX_TIMEOUT = int(os.environ.get("INSANE_REVIEW_REPOMIX_TIMEOUT", "300"))

CHATGPT_URL = "https://chatgpt.com/"


def _guard_dialogs(ctx, page=None):
    """Stop playwright's default dialog auto-dismiss from racing over CDP.

    Over connect_over_cdp, any JS dialog (beforeunload/alert/confirm) on the
    ChatGPT page triggers playwright's built-in auto-dismiss. Across CDP that
    races the browser → `ProtocolError: No dialog is showing`, an UNCAUGHT
    driver exception that crashes the run (100% CPU spin) before the prompt is
    ever submitted. Registering our own handler disables the default and
    swallows the race.
    """
    def _on_dialog(d):
        try:
            d.dismiss()
        except Exception:
            pass
    def _attach(p):
        try:
            p.on("dialog", _on_dialog)
        except Exception:
            pass
    try:
        for p in (getattr(ctx, "pages", None) or []):
            _attach(p)
        ctx.on("page", _attach)   # cover future tabs/pages too
    except Exception:
        pass
    if page is not None:
        _attach(page)


INPUT_SELECTORS = ['div[contenteditable="true"][role="textbox"][data-composer-markdown]', "#prompt-textarea"]
FILE_INPUT_SELECTOR = 'input[type="file"]'
# 폴백 리스트(첫 항목=현행 실측 셀렉터, 이후=구조적 폴백) — INPUT_SELECTORS와 같은 컨벤션
COPY_BTN_SELECTORS = [
    'button[type="button"][aria-label="복사"]',
    'button[data-testid="copy-turn-action-button"]',
    'button[aria-label="Copy"]',
    'button[data-testid*="copy"]',
]
STREAMING_BTN_SELECTORS = [
    'button[type="button"][aria-label="중지"]',
    'button[data-testid="stop-button"]',
    'button[aria-label="Stop streaming"]',
    'button[data-testid*="stop"]',
]
USER_MSG_SELECTORS = ['[data-chatgpt-search-unit-key$=":user"]', '[data-content-search-unit-key$=":user"]', '[data-message-author-role="user"]']
ASSISTANT_MSG_SELECTORS = ['[data-chatgpt-search-unit-key$=":assistant"]', '[data-content-search-unit-key$=":assistant"]', '[data-message-author-role="assistant"]']
# 턴 컨테이너(실측 2026-08-25: section[data-turn]) — copy 툴바는 메시지 div 바깥, 이 컨테이너 안에 있다
TURN_CONTAINER_SELECTOR = 'section[data-turn], article[data-turn], [data-turn]'

# 사용량 한도(쿼터) 차단 배너 감지 문구 — dialog/alert 표면에서만 대조(오탐 방지). 자유롭게 추가.
QUOTA_HINTS = [
    "usage limit", "reached your limit", "limit reached", "you've hit",
    "reached the current usage cap", "try again later", "upgrade to",
    "사용량 한도", "한도에 도달", "사용 한도", "요금제를 업그레이드",
]


def _q(page, selectors):
    """폴백 리스트에서 첫 매치 노드(없으면 None)."""
    for sel in selectors:
        try:
            node = page.query_selector(sel)
        except Exception:
            continue
        if node is not None:
            return node
    return None


def _qa(page, selectors):
    """폴백 리스트에서 첫 비어있지 않은 query_selector_all 결과(없으면 [])."""
    for sel in selectors:
        try:
            nodes = page.query_selector_all(sel)
        except Exception:
            continue
        if nodes:
            return nodes
    return []


def detect_quota_block(page):
    """쿼터/한도 차단 감지(보수적 — role=dialog/alert 표면만 스캔, 본문 응답 텍스트는 안 봄).
    매칭된 문구를 반환, 없으면 None. 실패는 조용히 None(대기 루프를 깨지 않음)."""
    try:
        for sel in ('[role="dialog"]', '[role="alert"]'):
            for node in page.query_selector_all(sel):
                if not node.is_visible():
                    continue
                txt = (node.inner_text() or "").strip()
                if not txt:
                    continue
                low = txt.lower()
                for hint in QUOTA_HINTS:
                    if hint.lower() in low:
                        return txt[:200]
    except Exception:
        return None
    return None
LOGIN_WALL_SELECTORS = [
    'button[data-testid="login-button"]',
    'a[href*="auth/login"]',
    'button:has-text("로그인")',
    'button:has-text("Log in")',
]

MAX_WAIT_SECS = int(os.environ.get("INSANE_REVIEW_MAX_WAIT", "1200"))  # 기본 20분(--max-wait/env로 변경)
MIN_WAIT_SECS = 20
STABLE_CHECK_SECS = 8
STATUS_INTERVAL = 15
FORCE_MAX_TRIES = 6    # force-answer 클릭 재시도 상한
STALL_RELOAD_SECS = int(os.environ.get("INSANE_REVIEW_STALL_RELOAD", "45"))  # 빈 턴·스트리밍 없음 지속 시 재로드까지
STALL_MAX_RELOADS = 3
# '지금 답변 받기' 버튼(cot v5 UI, 실측 2026-07-19): 본문 리즈닝 고정행 안의 button.
ANSWER_NOW_ROW_SELECTOR = 'div[data-testid="cot-v5-pinned-row"]'
ANSWER_NOW_TEXT_RE = re.compile(r"답변\s*받기|Get answer|answer now", re.I)
# 최대 대기 소진 시 마지막 수단으로 '지금 답변 받기'를 누른 뒤 답변 플러시를 기다리는 추가 유예.
FORCE_TIMEOUT_GRACE_SECS = int(os.environ.get("INSANE_REVIEW_FORCE_GRACE", "240"))

# --- v0.6.0 identity 결속 ---
# 전송이 만든 '대화 URL'(/c/<id>)에 회수를 결속한다. count 델타는 페이지가 다른 채팅을
# 보여주는 순간 무너진다(2026-07-18 스테일 캡처 실측) — URL 결속이 1차 방어, id-diff가 2차.
CONV_URL_RE = re.compile(r"/c/[0-9a-f]{8}[0-9a-f-]{4,}", re.I)
CONV_URL_CAPTURE_SECS = int(os.environ.get("INSANE_REVIEW_URL_CAPTURE_SECS", "90"))
VISIBLE_ERROR_GRACE_SECS = 3
# Pro 추론단계는 20~60분이 정상 범위(실측) — Pro 선택·검증 시 기본 최대 대기를 자동 상향.
# 사용자가 --max-wait 또는 INSANE_REVIEW_MAX_WAIT를 명시하면 그 값이 우선.
PRO_MAX_WAIT_SECS = int(os.environ.get("INSANE_REVIEW_PRO_MAX_WAIT", "3600"))
# 프로젝트 그룹핑 시 이전 채팅/파일 오염 방지 한 줄(패킹 첨부 전송에만 부착).
PROJECT_SCOPE_GUARD = ("\n\n(참고: 이번 메시지에 첨부된 파일만 근거로 답하라. "
                       "이 프로젝트의 이전 채팅·파일은 이번 과제와 무관하다.)")
# 첨부 실패 시 pack을 프롬프트에 인라인으로 붙여 보내는 폴백의 크기 상한(초과 시 자르지 않고 중단).
PASTE_FALLBACK_MAX_CHARS = int(os.environ.get("INSANE_REVIEW_PASTE_MAX", "50000"))

# 출력은 '실행한 현재 프로젝트'의 .insane-review/ 에 저장(플러그인 내부 X — kkirikkiri의 .kkirikkiri 패턴).
# env INSANE_REVIEW_OUT 또는 --out-dir로 오버라이드.
OUT_DIR = Path(os.environ["INSANE_REVIEW_OUT"]).expanduser() if os.environ.get("INSANE_REVIEW_OUT") \
    else Path.cwd() / ".insane-review"

DEFAULT_PROMPT = (
    "첨부는 repomix로 패킹한 코드베이스입니다. 다음을 한국어로 분석해줘:\n"
    "1) 이 프로젝트가 하는 일과 전체 아키텍처\n"
    "2) 핵심 모듈 간 데이터 흐름\n"
    "3) 잠재적 버그/리스크 또는 개선점 3가지 (근거 파일 경로 포함)\n"
    "결론부터 말하고 근거는 그 뒤에."
)


# ===========================================================================
# 1) repomix 패킹 (버전 핀 + timeout + returncode + 권한 + 시크릿 노트)
# ===========================================================================
class PackingCancelled(BaseException):
    pass


class PublicationUnknown(BaseException):
    pass


class PackOperation:
    def __init__(self):
        self.cancelled = False
        self.outcome = "DEFINITELY_NOT_COMMITTED"
        self.path = None
        self.cleanup_failed = False
        self.record = {}
        self.record_root = None


_PACK_OPERATION = ContextVar("insane_review_pack_operation", default=None)


def pack_repo(target: Path, *, include: str | None, ignore: str | None,
              compress: bool, style: str, token_budget: int | None,
              out_path: Path, line_numbers: bool = True) -> tuple[Path, int | None]:
    if shutil.which("npx") is None:
        sys.exit("❌ npx가 없습니다. Node.js를 설치하세요.")

    # 시크릿 위생: 대상에 secretlint(보안검사)를 끄는 로컬 repomix 설정이 있으면 외부전송 전 중단(fail-closed)
    for cfg in ("repomix.config.json", "repomix.config.json5", "repomix.config.jsonc"):
        p = target / cfg
        if p.exists():
            try:
                raw = p.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                sys.exit(f"❌ {cfg} 읽기 실패({str(exc)[:60]}) — 보안설정 검증 불가로 중단(fail-closed).")
            # 키/값의 따옴표 유무(JSON 쌍따옴표 / JSON5 무따옴표·단따옴표) 모두 매칭
            if re.search(r"""['"]?enableSecurityCheck['"]?\s*:\s*false""", raw):
                sys.exit(f"❌ {cfg}에서 보안검사(enableSecurityCheck)가 꺼져 있음 — 시크릿 유출 위험으로 중단.\n"
                         "     보안검사를 켜거나 해당 설정을 제거한 뒤 다시 실행하세요.")

    if compress:
        print("  ⚠️  --compress: 함수 본문이 제거된다(시그니처 골격만). 정확성 리뷰/원인분석엔 부적합 —\n"
              "       리뷰면 끄고, 너무 크면 --include로 관련 파일만 좁혀 풀로 보내라.")

    spec = f"repomix@{REPOMIX_VERSION}" if REPOMIX_VERSION else "repomix@latest"
    # hermetic: 외부 repomix 설정(CWD의 .ts/.js/json·글로벌 설정)이 압축·본문생략(output.files)·
    # 보안검사를 조용히 바꾸지 못하도록 안전한 임시 config를 만들어 --config로 강제한다
    # (--config 지정 시 repomix는 자동탐색 대신 이 파일을 쓴다). compress는 요청값만 반영.
    hermetic_cfg = {
        "output": {"compress": bool(compress), "files": True,
                   "removeComments": False, "removeEmptyLines": False},
        "security": {"enableSecurityCheck": True},
    }
    cfg_path = out_path.with_name(out_path.name + ".repomixcfg.json")
    try:
        cfg_path.write_text(json.dumps(hermetic_cfg), encoding="utf-8")
    except OSError:
        cfg_path = None
    cmd = ["npx", "-y", spec, str(target), "-o", str(out_path), "--style", style]
    if cfg_path is not None:
        cmd += ["--config", str(cfg_path)]   # 외부 설정 차단(압축·보안·본문생략 강제)
    if line_numbers:
        cmd.append("--output-show-line-numbers")  # AI가 파일:라인 인용 가능 → 근거 강제에 필요
    if compress:
        cmd.append("--compress")
    if include:
        cmd += ["--include", include]
    if ignore:
        cmd += ["--ignore", ignore]
    if token_budget:
        cmd += ["--token-budget", str(token_budget)]

    print(f"  $ {' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=REPOMIX_TIMEOUT)
    except subprocess.TimeoutExpired:
        if cfg_path is not None:
            try:
                cfg_path.unlink()
            except OSError:
                pass
        # 타임아웃 전에 repomix가 부분 산출물을 남겼으면 권한 축소(시크릿 위생 — 모든 실패경로 보장)
        if out_path.exists():
            try:
                os.chmod(out_path, 0o600)
            except OSError:
                pass
        sys.exit(f"❌ repomix 타임아웃({REPOMIX_TIMEOUT}s) — 네트워크/범위 확인")
    if cfg_path is not None:   # hermetic 임시 config 정리(성공 경로)
        try:
            cfg_path.unlink()
        except OSError:
            pass
    out = proc.stdout + proc.stderr

    tokens = None
    m = re.search(r"Total Tokens:\s*([\d,]+)", out)
    if m:
        tokens = int(m.group(1).replace(",", ""))

    # 시크릿 스캔 결과 노출 (repomix는 secretlint 기본 on — hit 파일은 출력에서 제외됨)
    sm = re.search(r"(\d+)\s+suspicious file", out)
    if sm and int(sm.group(1)) > 0:
        print(f"  🔒 secretlint: 의심 파일 {sm.group(1)}개 감지 → 출력에서 제외됨(외부 전송 안전)")

    if proc.returncode != 0:
        # 실패해도 repomix가 산출물을 남겼으면 권한 축소(token-budget 초과 시 파일 생성됨 — 시크릿 위생)
        if out_path.exists():
            try:
                os.chmod(out_path, 0o600)
            except OSError:
                pass
        if token_budget and tokens and tokens > token_budget:
            sys.exit(f"⚠️ 중단: 토큰 예산 초과 — 패킹은 완료됐으나 {tokens:,} > {token_budget:,} 한도. "
                     "범위를 좁히거나(--include) 예산을 늘리세요(--token-budget). [요청한 예산 가드]")
        else:
            sys.exit(f"❌ repomix 실행 실패 (rc={proc.returncode}) — 로그를 확인하세요.\n"
                     "     " + "\n     ".join(out.strip().splitlines()[-6:]))

    if not out_path.exists():
        sys.exit("❌ repomix 출력 파일이 생성되지 않았습니다.")

    # 외부 웹 서비스로 나가는 파일 → 권한 축소
    try:
        os.chmod(out_path, 0o600)
    except OSError:
        pass

    size = out_path.stat().st_size
    print(f"  ✓ 패킹 완료: {out_path.name}  ({size:,} bytes"
          + (f", ~{tokens:,} tokens)" if tokens else ")"))

    # 누락 검증(감사): 패킹된 파일 수/목록 노출 → 빠진 게 있으면 눈에 띄게
    mf = re.search(r"Total Files:\s*([\d,]+)", out)          # repomix stdout(신뢰가능 카운트)
    n_files = int(mf.group(1).replace(",", "")) if mf else None
    flist = []
    try:
        body = out_path.read_text(encoding="utf-8", errors="replace")
        if style == "markdown":                              # 구조 헤더 '## File:'는 컬럼0(라인번호 없음)
            flist = re.findall(r"(?m)^## File:\s+(.+?)\s*$", body)
    except OSError:
        pass
    cnt = n_files if n_files is not None else len(flist)
    shown = (": " + ", ".join(flist[:10]) + (f" … (+{len(flist) - 10})" if len(flist) > 10 else "")) if flist else ""
    print(f"  📦 패킹 포함 {cnt}개 파일{shown}")
    # 빈/불명 컨텍스트 전송 방지 — 파일수가 0이거나, 신뢰가능 카운트도 목록도 못 얻으면 중단(fail-closed)
    if n_files == 0 or (n_files is None and len(flist) == 0):
        try:
            os.chmod(out_path, 0o600)
        except OSError:
            pass
        reason = "0개" if n_files == 0 else "확인 불가(repomix 파일수 파싱 실패)"
        sys.exit(f"❌ 패킹 파일 수 {reason} — 대상 경로/--include/--ignore를 확인하세요(빈·불명 컨텍스트 전송 방지).")
    if compress:
        print("  ⚠️  위 파일들은 본문이 압축됨(⋮----) — 제어흐름 누락. 리뷰엔 부적합.")
    if tokens and tokens > 120_000:
        print(f"  ⚠️  pack이 큼(~{tokens:,} 토큰) — ChatGPT 웹에서 잘릴(truncation) 수 있다. "
              "--include로 좁히거나 여러 번 나눠 보내라.")
    return out_path, tokens


# ===========================================================================
# 2) 브라우저(CDP) 준비 + fail-closed 검증
# ===========================================================================
def is_port_open(port: int = CDP_PORT) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def cdp_browser_ok() -> bool:
    """포트가 '진짜 CDP 브라우저'인지 /json/version으로 검증(엉뚱한 프로세스 차단)."""
    try:
        with urllib.request.urlopen(f"{CDP_URL}/json/version", timeout=4) as r:
            info = json.loads(r.read().decode("utf-8"))
        browser = str(info.get("Browser", ""))
        return any(k in browser for k in ("Chrome", "Chromium", "Comet", "HeadlessChrome", "Edg"))
    except Exception:
        return False


# ---- 크로스플랫폼 브라우저 레지스트리 (mac / windows / linux) ----
def host_os() -> str:
    s = platform.system()
    return "mac" if s == "Darwin" else "win" if s == "Windows" else "linux"


# Arc은 CDP/멀티인스턴스가 불안정해 자동 목록에서 제외(사용자가 절대경로로 직접 지정은 가능).
def _browser_registry() -> list[tuple[str, list[str]]]:
    """[(표시이름, [후보 실행경로...])] — OS별. 절대경로는 존재검사, 비절대는 PATH(which)로 해석."""
    osname = host_os()
    home = Path.home()
    if osname == "mac":
        A = "/Applications"
        return [
            ("Chrome",   [f"{A}/Google Chrome.app/Contents/MacOS/Google Chrome"]),
            ("Comet",    [f"{A}/Comet.app/Contents/MacOS/Comet"]),
            ("Brave",    [f"{A}/Brave Browser.app/Contents/MacOS/Brave Browser"]),
            ("Edge",     [f"{A}/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]),
            ("Chromium", [f"{A}/Chromium.app/Contents/MacOS/Chromium"]),
            ("Vivaldi",  [f"{A}/Vivaldi.app/Contents/MacOS/Vivaldi"]),
        ]
    if osname == "win":
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pfx = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        lad = os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local"))
        return [
            ("Chrome",   [rf"{pf}\Google\Chrome\Application\chrome.exe",
                          rf"{pfx}\Google\Chrome\Application\chrome.exe",
                          rf"{lad}\Google\Chrome\Application\chrome.exe"]),
            ("Edge",     [rf"{pf}\Microsoft\Edge\Application\msedge.exe",
                          rf"{pfx}\Microsoft\Edge\Application\msedge.exe"]),
            ("Brave",    [rf"{pf}\BraveSoftware\Brave-Browser\Application\brave.exe",
                          rf"{pfx}\BraveSoftware\Brave-Browser\Application\brave.exe",
                          rf"{lad}\BraveSoftware\Brave-Browser\Application\brave.exe"]),
            ("Chromium", [rf"{lad}\Chromium\Application\chrome.exe"]),
            ("Vivaldi",  [rf"{lad}\Vivaldi\Application\vivaldi.exe"]),
        ]
    return [  # linux
        ("Chrome",   ["google-chrome", "google-chrome-stable"]),
        ("Chromium", ["chromium", "chromium-browser"]),
        ("Brave",    ["brave-browser", "brave"]),
        ("Edge",     ["microsoft-edge", "microsoft-edge-stable"]),
        ("Vivaldi",  ["vivaldi", "vivaldi-stable"]),
    ]


def detect_browsers() -> list[tuple[str, str]]:
    """이 OS에 설치된 크로미움 계열 브라우저 [(이름, 실행경로)]. env 경로 오버라이드도 우선 반영."""
    found, seen = [], set()
    for env, nm in (("INSANE_REVIEW_BROWSER_PATH", None),
                    ("INSANE_REVIEW_CHROME", "Chrome"), ("INSANE_REVIEW_COMET", "Comet")):
        p = os.environ.get(env)
        if p and Path(p).exists():
            name = nm or Path(p).stem
            if name.lower() not in seen:
                found.append((name, p)); seen.add(name.lower())
    for name, cands in _browser_registry():
        if name.lower() in seen:
            continue
        for c in cands:
            p = c if os.path.isabs(c) else (shutil.which(c) or "")
            if p and Path(p).exists():
                found.append((name, p)); seen.add(name.lower())
                break
    return found


def _load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_config_key(key: str, value) -> None:
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        cfg = _load_config()
        cfg[key] = value
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, CONFIG_PATH)
    except Exception:
        pass


def save_browser_choice(name_or_path: str) -> None:
    """선택한 브라우저(이름 또는 경로)를 config에 영속화 → 다음 실행부터 재질문 안 함."""
    _save_config_key("browser", name_or_path)


LAUNCH_MODES = ("foreground", "background", "headless")


def save_launch_mode(mode: str) -> None:
    """전용 브라우저를 어떻게 띄울지 영속화(최초 1회 선택 → 이후 재질문 없음).

    foreground : 기존 동작 — 창이 뜨고 포커스를 가져간다(진행 상황을 눈으로 보고 싶을 때)
    background : 창을 숨긴 채 실행(macOS `open -g` + 새 탭 생성 후 재숨김). **기본값** — 작업 흐름을 안 끊는다
    headless   : 창 자체가 없다. 가장 조용하지만 ChatGPT가 헤드리스를 차단하면 로그인/전송이 실패할 수 있어
                 --check-env로 검증된 환경에서만 권장
    """
    if mode not in LAUNCH_MODES:
        return
    _save_config_key("launch_mode", mode)


def hide_browser_if_background() -> None:
    """background 모드에서 브라우저 앱을 다시 숨긴다.

    `open -g`로 조용히 띄워도 playwright가 `ctx.new_page()`로 새 탭을 만드는 순간
    macOS가 그 앱을 앞으로 끌어올린다. 탭 생성 직후 이걸 호출해 다시 내린다
    (앱만 숨길 뿐 프로세스·CDP 세션은 그대로라 자동화는 계속 동작한다)."""
    if host_os() != "mac" or get_launch_mode() != "background":
        return
    proc = (_load_config().get("launch_proc_name") or "").strip()
    if not proc:
        return
    try:
        subprocess.run(
            ["osascript", "-e",
             f'tell application "System Events" to set visible of process "{proc}" to false'],
            capture_output=True, timeout=5)
    except Exception:
        pass


def get_launch_mode() -> str:
    env = (os.environ.get("INSANE_REVIEW_LAUNCH_MODE") or "").strip().lower()
    if env in LAUNCH_MODES:
        return env
    mode = (_load_config().get("launch_mode") or "").strip().lower()
    # 미설정 기본값은 background — 창이 안 보이면서도 ChatGPT가 정상 브라우저로 인식한다.
    # (headless는 컴포저를 못 받아 전송 실패, foreground는 포커스를 뺏어 작업 흐름을 끊는다. 2026-08-26 실측)
    return mode if mode in LAUNCH_MODES else "background"


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "browser"


def profile_dir_for(name: str, *, persist_owner: bool = True) -> Path:
    """브라우저별 전용 프로필 분리. 크로미움 계열은 브라우저(앱)마다 쿠키 암호화 키가 달라
    (mac Keychain 'X Safe Storage' 항목이 앱별), 같은 user-data-dir을 다른 브라우저로 열면
    기존 세션 쿠키가 복호화 불가 → 로그인이 통째로 깨진다. 기존 프로필(BROWSER_PROFILE_DIR)은
    최초 사용 브라우저(owner)가 계속 소유해 기존 로그인을 보존하고, 다른 브라우저는
    'browser-profile-<이름>' 접미사 디렉토리를 쓴다."""
    cfg = _load_config()
    owner = cfg.get("profile_owner")
    if not owner:
        # 소유자 미기록: 레거시 프로필이 있으면 저장된 browser(없으면 이번 브라우저)가 승계
        owner = (cfg.get("browser") if BROWSER_PROFILE_DIR.exists() else None) or name
        if persist_owner:
            _save_config_key("profile_owner", owner)
    # owner가 절대경로로 저장됐을 수 있음(--browser <경로>) → 등록된 브라우저 이름으로 정규화(없으면 stem)
    if os.path.isabs(str(owner)):
        resolved = resolve_browser(str(owner))
        owner_name = resolved[0] if resolved else Path(owner).stem
    else:
        owner_name = str(owner)
    if _slug(owner_name) == _slug(name):
        return BROWSER_PROFILE_DIR
    return BROWSER_PROFILE_DIR.with_name(f"{BROWSER_PROFILE_DIR.name}-{_slug(name)}")


def resolve_browser(name_or_path: str | None) -> tuple[str, str] | None:
    """--browser 값(이름 'chrome' 또는 절대경로)을 (이름, 경로)로 해석.
    인자 없으면 config 저장값 → 첫 감지 브라우저 순. 못 찾으면 None."""
    if name_or_path:
        if os.path.isabs(name_or_path) and Path(name_or_path).exists():
            # 등록된 브라우저와 같은 실행파일이면 등록된 이름으로 정규화한다. 파일명(stem)을 이름으로 쓰면
            # `--browser Chrome`과 `--browser <Chrome 절대경로>`가 서로 다른 프로필로 갈라진다(독립 리뷰 F3).
            for name, path in detect_browsers():
                try:
                    if os.path.samefile(path, name_or_path):
                        return (name, name_or_path)
                except OSError:
                    continue
            return (Path(name_or_path).stem, name_or_path)
        for name, path in detect_browsers():
            if name.lower() == name_or_path.lower():
                return (name, path)
        return None
    saved = _load_config().get("browser")
    if saved:
        r = resolve_browser(saved)
        if r:
            return r
    bs = detect_browsers()
    return bs[0] if bs else None


def launch_browser_exe(path: str, name: str | None = None) -> bool:
    """전용 프로필 + 디버그 포트로 크로미움 직접 실행(크로스플랫폼) 후 CDP가 뜰 때까지 대기.
    전용 프로필에 스테일 인스턴스가 떠 있어 새 런치가 포트를 못 여는 경우(같은 user-data-dir
    싱글톤 교착)를 감지해 그 프로세스를 정리하고 1회 재시도한다.
    프로필은 브라우저별로 분리(profile_dir_for) — 다른 브라우저가 같은 프로필을 열어
    쿠키 암호화 키 불일치로 로그인이 깨지는 것을 막는다."""
    profile_dir = profile_dir_for(name or Path(path).stem)
    try:
        profile_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    mode = get_launch_mode()
    # macOS에서 앱을 다시 숨기려면 System Events용 프로세스명이 필요하다(실행파일 basename).
    _save_config_key("launch_proc_name", Path(path).name)
    cmd = [path, f"--remote-debugging-port={CDP_PORT}",
           f"--user-data-dir={profile_dir}",
           "--no-first-run", "--no-default-browser-check"]
    if mode == "headless":
        # 신형 헤드리스만 CDP·쿠키가 정상 동작한다(구형 --headless는 로그인 세션이 깨짐)
        cmd.append("--headless=new")

    def _spawn_and_wait(secs: int) -> bool:
        try:
            if mode == "background" and host_os() == "mac":
                # `open -g`: 창은 뜨되 포커스를 가져가지 않아 사용자의 작업 흐름을 끊지 않는다.
                # -n(새 인스턴스)로 전용 프로필이 기존 창에 흡수되는 것을 막는다.
                subprocess.Popen(["open", "-g", "-n", "-a", path, "--args", *cmd[1:]],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            print(f"  ❌ 실행 실패: {str(exc)[:80]}")
            return False
        for i in range(secs):
            if is_port_open() and cdp_browser_ok():
                print(f"  ✓ 시작 완료 ({i + 1}s)")
                time.sleep(2)
                return True
            time.sleep(1)
        return False

    print(f"  브라우저 시작: {Path(path).name} (CDP {CDP_PORT}, 전용 프로필 {profile_dir.name})")
    if _spawn_and_wait(15):
        return True
    # 포트 미개방 = 전용 프로필에 떠 있던 스테일 인스턴스가 런치를 흡수했을 가능성.
    # 그 프로세스를 정리(로그인 보존)하고 싱글톤 락이 풀리길 기다린 뒤 1회 재시도.
    print("  ⚠️  디버그 포트 미개방 — 전용 프로필 스테일 인스턴스 정리 후 재시도")
    _kill_profile_browsers(profile_dir)
    time.sleep(3)
    if _spawn_and_wait(20):
        return True
    print("  ❌ 브라우저 시작 타임아웃 (전용 프로필 정리 후에도 실패)")
    return False


def _kill_profile_browsers(profile_dir: Path) -> None:
    """전용 프로필을 점유 중인 브라우저 프로세스를 정리(크로스플랫폼 best-effort).
    전용 프로필이라 종료해도 로그인 쿠키는 디스크에 보존된다 — 스테일 인스턴스가
    새 런치를 흡수해(같은 user-data-dir 싱글톤) 디버그 포트가 안 열리는 교착을 푼다."""
    target = str(profile_dir)
    try:
        if host_os() == "win":
            ps = ("Get-CimInstance Win32_Process | "
                  f"Where-Object {{ $_.CommandLine -like '*{target}*' }} | "
                  "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
                  "-ErrorAction SilentlyContinue }")
            subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, timeout=15)
        else:
            subprocess.run(["pkill", "-f", target], capture_output=True, timeout=10)
    except Exception:
        pass


def _restart_profile_browser() -> bool:
    """전용 프로필 브라우저를 재기동(쿠키는 디스크 보존 → 로그인 유지)."""
    saved = _load_config().get("browser")
    r = resolve_browser(saved) if saved else resolve_browser(None)
    if not r:
        return False
    _kill_profile_browsers(profile_dir_for(r[0]))
    time.sleep(3)
    return launch_browser_exe(r[1], r[0])



def ensure_browser(browser_arg: str | None) -> bool:
    """이미 CDP가 떠 있으면 그걸 검증·사용, 아니면 지정/감지된 브라우저를 전용 프로필로 띄운다."""
    if is_port_open():
        if cdp_browser_ok():
            print(f"  ✓ CDP 브라우저 확인 (port {CDP_PORT})")
            return True
        print(f"  ❌ port {CDP_PORT}에 CDP 브라우저가 아닌 다른 프로세스가 떠 있음")
        return False
    resolved = resolve_browser(browser_arg)
    if not resolved:
        avail = ", ".join(n for n, _ in detect_browsers()) or "없음"
        print(f"  ❌ 사용할 브라우저를 찾지 못함 (지정='{browser_arg}', 설치감지=[{avail}])")
        return False
    return launch_browser_exe(resolved[1], resolved[0])


# 실행 중 브라우저가 디스크에서 자동 업데이트되면(스테일 인스턴스) CDP 연결이 이 에러로 깨진다.
# 실측(2026-07-09, Chrome 150.46 실행 중 + 디스크 150.101): connect_over_cdp가 아래 메시지로 실패.
_STALE_CDP_MARKERS = ("Browser context management is not supported",)


def connect_cdp(pw):
    """connect_over_cdp + 스테일 브라우저 자동 복구.
    브라우저가 떠 있는 동안 자동 업데이트되면 CDP가 깨진다(위 마커). 이때 전용 프로필
    프로세스만 재기동(로그인 보존)하고 1회 재연결 — 사용자에게 '로그인 풀림'으로 보이던
    상황의 상당수가 이 스테일 케이스다."""
    try:
        return pw.chromium.connect_over_cdp(CDP_URL)
    except Exception as exc:
        if not any(m in str(exc) for m in _STALE_CDP_MARKERS):
            raise
        print("  ♻️  CDP 연결 실패(스테일 브라우저 — 실행 중 자동업데이트 추정) → 전용 브라우저 재기동(로그인 보존)")
        if not _restart_profile_browser():
            raise
        return pw.chromium.connect_over_cdp(CDP_URL)


def _cookie_state(ctx) -> tuple[str, str]:
    """세션 쿠키(__Secure-next-auth.session-token*)의 존재·만료를 확인.
    반환: (state, expiry) — state ∈ 'ok' | 'expired' | 'missing' | 'unknown'.
    UI 프로브가 흔들려도(로딩/CF 챌린지) 쿠키로 '세션 자체'의 생사를 진단하기 위한 것."""
    try:
        cookies = ctx.cookies("https://chatgpt.com")
    except Exception:
        return ("unknown", "-")
    toks = [c for c in cookies
            if str(c.get("name", "")).startswith("__Secure-next-auth.session-token")]
    if not toks:
        return ("missing", "-")
    exp = max(float(c.get("expires") or 0) for c in toks)
    if exp <= 0:
        return ("ok", "session")   # 만료 미설정(세션 쿠키)
    exp_s = datetime.fromtimestamp(exp).strftime("%Y-%m-%d")
    return (("ok" if exp > time.time() else "expired"), exp_s)


def probe_login() -> dict:
    """브라우저(CDP) up + playwright 있을 때 ChatGPT 로그인 상태를 확인.
    반환: {'login': 'ok'|'no'|'unknown', 'cookie': 'ok'|'expired'|'missing'|'unknown', 'cookie_exp': str}
    - login='no'는 로그인 벽이 실제로 보일 때만. 컴포저가 늦게 떠도(SPA 로딩/CF 챌린지) 'no'로
      오판하지 않고 'unknown' — 멀쩡한 세션에 재로그인을 요구하던 거짓 음성 방지.
    - cookie는 UI와 무관하게 세션 쿠키의 생사를 별도 보고(진단용)."""
    import importlib.util
    res = {"login": "unknown", "cookie": "unknown", "cookie_exp": "-", "mode": "unknown"}
    if not (is_port_open(CDP_PORT) and cdp_browser_ok()):
        return res
    if not importlib.util.find_spec("playwright"):
        return res
    try:
        from playwright.sync_api import sync_playwright as _spw
        with _spw() as pw:
            b = connect_cdp(pw)
            ctx = pick_context(b)
            if ctx is None:
                res["login"], res["cookie"] = "no", "missing"
                return res

            res["cookie"], res["cookie_exp"] = _cookie_state(ctx)
            page = ctx.new_page()
            hide_browser_if_background()  # 새 탭 생성이 앱을 앞으로 끌어올리므로 즉시 재숨김
            _guard_dialogs(ctx, page)
            try:
                page.goto(CHATGPT_URL, wait_until="load", timeout=30000)
                res["login"] = login_state(page, wait_secs=15)
                if res["login"] == "ok":
                    # Chat/Work는 sticky이고 Work엔 Pro가 없다 — 진단에 현재 모드를 노출.
                    # 토글은 컴포저보다 늦게 렌더되므로 잠깐 기다렸다 읽는다.
                    m = "unknown"
                    for _ in range(12):
                        m = read_mode(page)
                        if m != "unknown":
                            break
                        time.sleep(0.5)
                    res["mode"] = mode_probe_value(m)
            finally:
                try:
                    page.close()
                except Exception:
                    pass
    except Exception:
        pass
    return res


def check_env(do_install: bool = False) -> int:
    """환경 점검 — node/npx, repomix, pyperclip, playwright, CDP 브라우저, ChatGPT 로그인.
    마지막에 'STATUS ...' 라인을 출력해 커맨드(AskUserQuestion 온보딩)가 분기에 파싱한다."""
    import importlib.util
    print("=== insane-review 환경 점검 ===")
    ok, issues = [], []

    npx, node = shutil.which("npx"), shutil.which("node")
    node_ok = bool(node and npx)
    if node_ok:
        ok.append("node/npx 있음")
        ok.append(f"repomix: `npx -y repomix@{REPOMIX_VERSION or 'latest'}`로 자동 설치(사전설치 불필요)")
    else:
        issues.append(("node/npx 없음", "Node.js 설치: https://nodejs.org 또는 `brew install node`"))

    # pip 의존성 — do_install이면 '로그인 프로브 전에' 먼저 설치(설치 후 프로브 가능)
    if do_install:
        for mod, pip in (("pyperclip", "pyperclip"), ("playwright", "playwright")):
            if not importlib.util.find_spec(mod):
                print(f"  [--install] pip install {pip} ...")
                subprocess.run([sys.executable, "-m", "pip", "install", pip])
        importlib.invalidate_caches()

    deps_ok = True
    for mod, pip in (("pyperclip", "pyperclip"), ("playwright", "playwright")):
        if importlib.util.find_spec(mod):
            ok.append(f"python {mod} 있음")
        else:
            issues.append((f"python {mod} 없음", f"pip install {pip} (또는 --install)"))
            deps_ok = False

    if is_port_open(CDP_PORT) and cdp_browser_ok():
        browser_state = "ok"
        ok.append(f"CDP 브라우저({CDP_PORT}) 확인")
    elif is_port_open(CDP_PORT):
        browser_state = "wrong"
        issues.append((f"port {CDP_PORT}이 CDP 브라우저 아님", "다른 프로세스 종료 후 --launch-browser로 전용 프로필 실행"))
    else:
        browser_state = "down"
        issues.append((f"브라우저 CDP({CDP_PORT}) 닫힘",
                       "전용 브라우저를 디버그포트+전용프로필로 실행(--launch-browser; 아래 BROWSERS 참고)"))

    # ChatGPT 로그인 프로브(브라우저 up + deps 있을 때만)
    probe = {"login": "unknown", "cookie": "unknown", "cookie_exp": "-", "mode": "unknown"}
    if browser_state == "ok" and deps_ok:
        probe = probe_login()
        if probe["login"] == "ok":
            ok.append("ChatGPT 로그인됨 (입력창/모델 어포던스 확인)")
        elif probe["login"] == "no":
            issues.append(("ChatGPT 로그인 안 됨 (로그인 벽 확인됨)",
                           "해당 브라우저에서 chatgpt.com 로그인 + Pro 추론 선택"))
        elif probe["cookie"] == "ok":
            # UI 미확인이지만 세션 쿠키는 살아있음 → 로그인 요구 대상 아님(로딩/챌린지 가능성)
            ok.append(f"ChatGPT 세션 쿠키 유효(만료 {probe['cookie_exp']}) — UI 확인만 지연(로딩/챌린지 가능), 재점검 권장")
        else:
            issues.append((f"ChatGPT 로그인 확인 불가 (login=unknown, cookie={probe['cookie']})",
                           "전용 브라우저 창에서 chatgpt.com 상태 확인 후 재점검"))

    for o in ok:
        print(f"  ✓ {o}")
    for name, hint in issues:
        print(f"  ✗ {name}\n      → {hint}")

    # 저장된 브라우저 선택값(있으면 이름) — 커맨드가 "최초 1회만 질문" 분기를 명시적으로 판단.
    _saved = _load_config().get("browser")
    if _saved:
        _r = resolve_browser(_saved)
        saved_browser = _r[0] if _r else _saved
    else:
        saved_browser = "none"

    # 머신 파싱용 상태 라인 — 커맨드 온보딩이 어느 단계가 막혔는지 분기에 사용(토큰 additive)
    print(f"\nSTATUS node={'ok' if node_ok else 'missing'} deps={'ok' if deps_ok else 'missing'} "
          f"browser={browser_state} login={probe['login']} cookie={probe['cookie']} "
          f"cookie_exp={probe['cookie_exp']} saved_browser={saved_browser} os={host_os()} "
          f"launch_mode={(_load_config().get('launch_mode') or 'unset')} "
          f"mode={probe.get('mode', 'unknown')}")
    # 설치된 크로미움 목록 — 커맨드가 브라우저 선택 AskUserQuestion을 구성하는 데 사용
    bs = detect_browsers()
    print("BROWSERS " + ",".join(n for n, _ in bs))
    print(f"결과: {len(ok)} OK / {len(issues)} 부족" + ("  — 전부 준비됨 ✅" if not issues else "  ⚠️"))
    return len(issues)


# ===========================================================================
# 3) ChatGPT 상호작용 프리미티브
# ===========================================================================
def find_input(page):
    for sel in INPUT_SELECTORS:
        try:
            el = page.query_selector(sel)
            if el:
                return el
        except Exception:
            continue
    return None


def _selector_union(selectors) -> str:
    """폴백 리스트를 CSS selector-list 하나로 — querySelectorAll은 같은 노드를 중복 반환하지 않는다.
    '첫 비영 셀렉터만 세는' 방식은 기준 시점과 현재 시점이 서로 다른 셀렉터를 세게 되어
    count-delta가 깨진다(2026-08-24 실측: 와일드카드 copy 1개 → 정밀 copy 1개 = '증가 없음' 오판)."""
    return selectors if isinstance(selectors, str) else ", ".join(selectors)


def count_msgs(page, selectors) -> int:
    try:
        return len(page.query_selector_all(_selector_union(selectors)))
    except Exception:
        return 0


def count_msgs_strict(page, selectors) -> int:
    """기준개수 포착 전용 — 조회 실패를 0으로 숨기지 않는다. 재시도 후에도 실패하면 예외(fail-closed).
    base_* 가 조회실패로 0이 되면 기존 DOM이 '새 턴'으로 오인돼 이전 답변을 저장할 수 있으므로 이를 차단한다."""
    last_exc = None
    for _ in range(3):
        try:
            return len(page.query_selector_all(_selector_union(selectors)))
        except Exception as exc:
            last_exc = exc
        time.sleep(0.3)
    raise RuntimeError(f"기준 메시지 수 조회 실패({selectors}): {str(last_exc)[:60]} → 전송 중단(fail-closed)")


def ui_adapter(page) -> str:
    if page.query_selector('button[data-codex-intelligence-trigger], [data-composer-markdown]'):
        return "current"
    if page.query_selector(INTELLIGENCE_PICKER_SELECTOR):
        return "legacy_slider"
    if page.query_selector('#prompt-textarea'):
        return "legacy"
    return "unsupported"


def streaming_state(page) -> str:
    try:
        if ui_adapter(page) == "unsupported":
            return "unknown"
        buttons = page.query_selector_all(_selector_union(STREAMING_BTN_SELECTORS))
        return "streaming" if any(b.is_visible() for b in buttons) else "absent"
    except Exception:
        return "unknown"


def is_streaming(page) -> bool:
    # Unknown must never become evidence of completion.
    return streaming_state(page) != "absent"


def node_ids(node) -> set[str]:
    raw = node.get_attribute("data-chatgpt-search-message-ids") or node.get_attribute("data-message-id") or ""
    return set(raw.split())


def message_nodes(page, role: str):
    kind = ui_adapter(page)
    if kind == "unsupported":
        raise RuntimeError("미지원 메시지 UI")
    selector = (f'[data-chatgpt-search-unit-key$=":{role}"], [data-content-search-unit-key$=":{role}"]'
                if kind == "current" else f'[data-message-author-role="{role}"]')
    return canonical_message_nodes(page.query_selector_all(selector), kind == "current")


def canonical_message_nodes(nodes, current):
    result, seen = [], set()
    for node in nodes:
        if current and not node_ids(node):
            node = _closest(node, '[data-chatgpt-search-message-ids]')
        if node is None or not node_ids(node):
            raise RuntimeError("메시지 identity 미확인")
        key = tuple(sorted(node_ids(node)))
        if key not in seen:
            result.append(node)
            seen.add(key)
    return result


def user_body_text(node) -> str:
    bubbles = node.query_selector_all('[data-user-message-bubble]')
    if len(bubbles) == 1:
        return bubbles[0].inner_text()
    if len(bubbles) > 1:
        raise RuntimeError("user 본문이 유일하지 않음")
    if node.get_attribute("data-content-search-unit-key"):
        return node.inner_text()
    bodies = node.query_selector_all('[data-content-search-unit-key$=":user"]')
    if len(bodies) == 1:
        return bodies[0].inner_text()
    if node.get_attribute("data-message-author-role") == "user":
        return node.inner_text()
    raise RuntimeError("user 본문 경계 미확인")


# 실측(2026-10-01): 긴 user 메시지는 접혀 표시되어 본문 끝에 "… 더 보기"가 붙고, 마크다운 렌더링이
# 인라인 코드의 백틱 등을 지워 표시 텍스트가 전송 텍스트와 달라진다. 표시 텍스트의 정확 해시 비교는
# 정상 전송을 "본문 불일치"로 오판한다. 그래서 전송 텍스트의 '지문'(해시만 저장, 평문 저장 없음)을 둔다:
#   lite     : 마크다운이 지우는 문자(` * _ ~ #)와 공백만 제거 — 연산자·구두점은 그대로 비교한다.
#   skeleton : 문자·숫자만 남김, numbers: 숫자 토큰 열, ops: 연산자 열
# lite가 일치하면 인정하고, 아니면 skeleton·numbers·ops가 '모두' 일치할 때만 인정한다(독립 리뷰 F2:
# 기호만 달라 의미가 바뀐 본문 — `x > 0`/`x < 0`, `3.5`/`35` — 을 승인하지 않기 위함).
_COLLAPSE_SUFFIX_RE = re.compile(r"\s*(?:…|\.\.\.)\s*(?:더 보기|Show more|See more|Read more)\s*$", re.I)
_ASSISTANT_LABEL_RE = re.compile(r"^\s*ChatGPT\s*(?:답변|said)\s*:\s*", re.I)
_MARKDOWN_ERASED_RE = re.compile(r"[`*_~#\s]+")
_NUMBER_TOKEN_RE = re.compile(r"\d(?:[\d.,:/-]*\d)?")
_OPERATOR_RE = re.compile(r"[<>=!+%&^|\\/@$]")


def message_fingerprint(text: str | None) -> dict:
    base = _COLLAPSE_SUFFIX_RE.sub("", text or "")
    parts = dict(
        lite=_MARKDOWN_ERASED_RE.sub("", base),
        skeleton=re.sub(r"[\W_]+", "", base, flags=re.UNICODE),
        numbers="|".join(_NUMBER_TOKEN_RE.findall(base)),
        ops="".join(_OPERATOR_RE.findall(base)),
    )
    return {key: hashlib.sha256(value.encode()).hexdigest() for key, value in parts.items()}


def fingerprint_matches(shown: str | None, stored) -> bool:
    if not isinstance(stored, dict):
        return False
    current = message_fingerprint(shown)
    if current["lite"] == stored.get("lite"):
        return True
    return all(current[key] == stored.get(key) for key in ("skeleton", "numbers", "ops"))


# 응답 결속/완료 판정이 던지는 고정 사유. 예외 메시지는 원칙적으로 출력하지 않는다(비밀/페이지 내용 유출 방지).
# 아래 문자열과 '정확히' 일치할 때만 사유를 보이며, 무엇이 덧붙은 메시지는 일치하지 않아 클래스 이름만 나간다.
SAFE_FAILURE_REASONS = frozenset({
    "전송 user 본문 불일치", "user 본문이 유일하지 않음", "user 본문 경계 미확인",
    "새 user 후보가 복수입니다", "해당 user의 assistant가 유일하지 않음", "assistant identity 없음",
    "회수 대상 assistant 변경 — 자동 재결속 금지", "기존 assistant를 새 응답으로 사용할 수 없음",
    "결속 대화 이탈", "메시지 identity 미확인", "공유 턴 user 모호", "user 턴 경계 미확인",
    "binding checkpoint 저장 실패", "미지원 메시지 UI",
})


def failure_detail(exc: BaseException) -> str:
    """실행 단계 실패의 정제된 사유: 고정 사유 목록과 정확히 일치하면 그 문구, 아니면 예외 클래스 이름만."""
    if type(exc) is RuntimeError and len(exc.args) == 1 and exc.args[0] in SAFE_FAILURE_REASONS:
        return exc.args[0]
    return type(exc).__name__


def error_surface_state(page) -> str:
    """Only visible semantic error surfaces count; lookup failures stay unknown."""
    try:
        for login in page.query_selector_all('button[data-testid="login-button"], a[href*="auth/login"]'):
            if login.is_visible():
                return "error"
        for node in page.query_selector_all('[role="alert"], [role="dialog"]'):
            if not node.is_visible():
                continue
            text = normalize(node.inner_text()).casefold()
            if not text:
                continue
            if any(h.casefold() in text for h in QUOTA_HINTS) or re.search(
                    r"오류|문제가 발생|로그인|한도|응답.*실패|something went wrong|error|log in|sign in|failed to|try again", text):
                return "error"
        return "clear"
    except Exception:
        return "unknown"


def msg_id_set(page) -> set:
    """Collect validated message IDs; an empty DOM is distinct from a failed query."""
    return set().union(*(node_ids(n) for role in ("user", "assistant") for n in message_nodes(page, role)))


def new_assistant_node(page, base_ids: set | None, base_assistant: int = 0):
    """회수 대상 assistant 노드. base_ids가 있으면 id 차집합의 마지막 신규 노드,
    없으면(레거시) 전송 전보다 노드가 늘었을 때만 마지막 노드. 없으면 None."""
    try:
        nodes = message_nodes(page, "assistant")
        if not nodes:
            return None
        if base_ids is None:
            return None
        # id가 없는 컨테이너(section/article 폴백)는 차집합 판정 불가 → 제외(옛 턴을 '신규'로 오인 방지)
        fresh = [n for n in nodes if node_ids(n) - base_ids]
        return fresh[0] if len(fresh) == 1 else None
    except Exception:
        return None


def _node_text(node) -> str:
    try:
        return (node.inner_text() or "") if node is not None else ""
    except Exception:
        return ""


def new_assistant_text(page, base_ids: set) -> str:
    """base_ids에 없는 '신규' assistant 턴의 텍스트(여럿이면 마지막). 없으면 ''."""
    return _node_text(new_assistant_node(page, base_ids))


def current_url(page) -> str:
    """페이지의 '실제' 현재 URL. page.url은 로컬 캐시라 CDP 왕복 없이는 SPA pushState를
    반영하지 못한다(실측 2026-07-23: 전송 후 30s 폴링에도 스테일, evaluate 1회로 즉시 갱신).
    location.href 평가가 1순위, 실패 시 page.url 폴백."""
    try:
        return page.evaluate("() => location.href") or ""
    except Exception:
        try:
            return page.url or ""
        except Exception:
            return ""


def capture_conv_url(page, timeout_secs: int = CONV_URL_CAPTURE_SECS) -> str | None:
    """전송 후 SPA가 발급하는 대화 URL(/c/<id>)을 포착. 실패 시 None(호출자 fail-closed)."""
    deadline = time.monotonic() + timeout_secs
    while time.monotonic() < deadline:
        u = current_url(page)
        if CONV_URL_RE.search(u):
            return u
        time.sleep(1)
    return None


def normalize(text: str | None) -> str:
    return re.sub(r"\s+", " ", text).strip() if text else ""


def last_assistant_node(page):
    nodes = _qa(page, ASSISTANT_MSG_SELECTORS)
    return nodes[-1] if nodes else None


def last_assistant_text(page) -> str:
    node = last_assistant_node(page)
    if node:
        try:
            return node.inner_text() or ""
        except Exception:
            return ""
    return ""


def node_copy_button(node):
    """해당 assistant 노드 '안'의 턴 복사 버튼(전역 마지막 버튼이 아님 — 코드블록 copy/다른 턴 오클릭 방지)."""
    if node is None:
        return None
    try:
        current = bool(node.get_attribute("data-chatgpt-search-unit-key") or node.get_attribute("data-content-search-unit-key"))
        boundary = '[data-turn-key]' if current else 'section[data-turn="assistant"], article[data-turn="assistant"]'
        scope = node.evaluate_handle("(n, sel) => n.closest(sel)", boundary).as_element()
        if scope is None:
            return None
        assistants = canonical_message_nodes(scope.query_selector_all(_selector_union(ASSISTANT_MSG_SELECTORS)), current)
        if len(assistants) != 1 or node_ids(assistants[0]) != node_ids(node):
            return None
        selector = ('button[type="button"][aria-label="복사"]' if current
                    else 'button[data-testid="copy-turn-action-button"]')
        buttons = scope.query_selector_all(selector)
        valid = [b for b in buttons if b.is_visible() and b.is_enabled() and not b.evaluate(
            '''(b) => !!b.closest('pre, code, [data-user-message-bubble], [data-message-author-role=user], [data-chatgpt-search-unit-key$=":user"], [data-content-search-unit-key$=":user"]')''')]
        if current:
            # 실측(2026-10-01): 턴 단위 복사 버튼은 응답 길이와 무관하게 어시스턴트 노드 '밖' 툴바
            # (복사/공유/소리 내어 읽기/응답 다시 생성/반응하기)에 있고, 코드 블록이 있으면 본문 노드 '안'에
            # 코드 블록 헤더의 "복사" 버튼이 따로 생긴다(pre 밖). 완료 증거는 노드 '밖'의 유일한 버튼뿐이다.
            valid = [b for b in valid if not b.evaluate("(b, n) => n.contains(b)", node)]
        return valid[0] if len(valid) == 1 else None
    except Exception:
        return None
    return None


def send_button_ready(page) -> bool:
    """컴포저가 다시 전송 가능 상태(=이전 턴 종결)인지. copy 툴바가 늦게 붙는 변형의 보조 종결 신호."""
    for sel in SEND_BTN_SELECTORS:
        try:
            for btn in page.query_selector_all(sel):
                if btn.is_visible() and btn.is_enabled():
                    return True
        except Exception:
            continue
    return False


def turn_terminal(page, node) -> bool:
    """Known streaming absence plus an unambiguous copy action in the bound turn."""
    if node is None or is_streaming(page):
        return False
    return node_copy_button(node) is not None


def clipboard_matches(txt: str, expected: str | None) -> bool:
    """Whole-content comparison retained for regression checks; harvest uses DOM only."""
    return bool(expected) and normalize(txt) == normalize(expected)


# ---- 모델 스위처 ----
MODEL_SWITCHER_SELECTORS = [
    'button[data-codex-intelligence-trigger="true"]',
    'button.__composer-pill[aria-haspopup="menu"]',   # 실측: 모델/추론 pill
    'button[data-testid="model-switcher-dropdown-button"]',
    'button[aria-label*="model" i]',
]
# 실측(2026-07-10): pill 클릭 → menuitemradio(즉시/중간/높음/매우 높음/Pro=추론단계)
#   + menuitem("GPT-5.6 Sol"=모델 서브메뉴 트리거). 트리거를 hover하면 모델 radio들
#   (GPT-5.6 Sol/GPT-5.5/GPT-5.4/GPT-5.3/o3)이 같은 메뉴 DOM에 menuitemradio로 추가된다.
# 실측(2026-08-18): UI 개편 — pill 팝오버가 슬라이더(simple 뷰)로 열린다.
#   [data-testid="composer-intelligence-picker-content"] 안에 '고급' menuitem이 있고,
#   클릭하면 advanced 뷰('모델' / '추론 강도' 서브메뉴 트리거)로 전환된다.
#   '추론 강도'를 hover하면 옛 menuitemradio 목록(즉시/중간/높음/매우 높음/Pro)이 그대로 뜬다.
#   활성 모델명은 '모델' 행의 trailing span(예: 'GPT-5.6 Sol')에 표시된다.
EFFORT_ITEM_SELECTORS = ['[role="menuitemradio"]', '[role="menuitem"]', '[role="option"]']
INTELLIGENCE_PICKER_SELECTOR = '[data-testid="composer-intelligence-picker-content"]'

# ---- Chat / Work 모드 (실측 2026-08-29, 2026-09-30) ----
# 구 UI는 radiogroup, 현 UI는 role=group 안의 Chat/Work 버튼 쌍이다. URL·_account 쿠키가 동일해
# workspace_id 결속으로는 구분되지 않는다. Work 모드엔 Pro 추론단계가 아예 없고
# (슬라이더에 Pro 눈금 부재, data-max="false") pill이 '5.6 Sol 매우 높음'으로 뜬다.
# 선택은 sticky — 사람이 웹에서 Work로 바꿔 쓰면 이후 자동 실행이 조용히 비-Pro로 나간다.
MODE_RADIO_SELECTOR = '[role="radiogroup"] [role="radio"]'
JS_READ_MODE = """() => {
  const norm = s => (s || '').trim().replace(/\\s+/g, ' ').toLowerCase();
  const modeNames = new Set(['chat', 'work']);
  const groups = [...document.querySelectorAll('[role="group"]')];
  const current = groups.map(g => {
    const buttons = [...g.querySelectorAll('button')];
    const named = buttons.map(b => ({b, label: norm(b.getAttribute('aria-label') || b.innerText || b.textContent)}));
    const labels = named.map(x => x.label);
    const groupLabel = norm(g.getAttribute('aria-label'));
    const recognizedGroup = /^(composer mode|작성기 모드)$/.test(groupLabel);
    return {g, named, candidate: labels.some(x => modeNames.has(x)) || recognizedGroup};
  }).filter(x => x.candidate);
  if (current.length) {
    if (current.length !== 1) return 'unknown';
    const named = current[0].named;
    const chat = named.filter(x => x.label === 'chat');
    const work = named.filter(x => x.label === 'work');
    if (chat.length !== 1 || work.length !== 1) return 'unknown';
    const c = chat[0].b.getAttribute('aria-pressed');
    const w = work[0].b.getAttribute('aria-pressed');
    if (!((c === 'true' && w === 'false') || (c === 'false' && w === 'true'))) return 'unknown';
    return c === 'true' ? 'chat' : 'work';
  }
  const legacy = [...document.querySelectorAll('[role="radiogroup"]')].map(g => {
    const radios = [...g.querySelectorAll('[role="radio"]')];
    const named = radios.map(r => ({r, label: norm(r.getAttribute('aria-label') || r.innerText || r.textContent)}));
    return {named, candidate: named.some(x => modeNames.has(x.label))};
  }).filter(x => x.candidate);
  if (!legacy.length) return 'absent';
  if (legacy.length !== 1) return 'unknown';
  const chat = legacy[0].named.filter(x => x.label === 'chat');
  const work = legacy[0].named.filter(x => x.label === 'work');
  if (chat.length !== 1 || work.length !== 1) return 'unknown';
  const c = chat[0].r.getAttribute('aria-checked');
  const w = work[0].r.getAttribute('aria-checked');
  if (!((c === 'true' && w === 'false') || (c === 'false' && w === 'true'))) return 'unknown';
  return c === 'true' ? 'chat' : 'work';
}"""
JS_CLICK_MODE = """(want) => {
  if (want !== 'Chat') return false;
  const norm = s => (s || '').trim().replace(/\\s+/g, ' ').toLowerCase();
  const modeNames = new Set(['chat', 'work']);
  const groups = [...document.querySelectorAll('[role="group"]')];
  const current = groups.map(g => {
    const named = [...g.querySelectorAll('button')].map(b => ({b, label: norm(b.getAttribute('aria-label') || b.innerText || b.textContent)}));
    const groupLabel = norm(g.getAttribute('aria-label'));
    return {named, candidate: named.some(x => modeNames.has(x.label)) || /^(composer mode|작성기 모드)$/.test(groupLabel)};
  }).filter(x => x.candidate);
  if (current.length) {
    if (current.length !== 1) return false;
    const chat = current[0].named.filter(x => x.label === 'chat');
    const work = current[0].named.filter(x => x.label === 'work');
    if (chat.length !== 1 || work.length !== 1 || chat[0].b.getAttribute('aria-pressed') !== 'false'
        || work[0].b.getAttribute('aria-pressed') !== 'true') return false;
    chat[0].b.click();
    return true;
  }
  const legacy = [...document.querySelectorAll('[role="radiogroup"]')].map(g => {
    const named = [...g.querySelectorAll('[role="radio"]')].map(r => ({r, label: norm(r.getAttribute('aria-label') || r.innerText || r.textContent)}));
    return {named, candidate: named.some(x => modeNames.has(x.label))};
  }).filter(x => x.candidate);
  if (legacy.length !== 1) return false;
  const chat = legacy[0].named.filter(x => x.label === 'chat');
  const work = legacy[0].named.filter(x => x.label === 'work');
  if (chat.length !== 1 || work.length !== 1 || chat[0].r.getAttribute('aria-checked') !== 'false'
      || work[0].r.getAttribute('aria-checked') !== 'true') return false;
  chat[0].r.click();
  return true;
}"""


def read_mode(page) -> str:
    """Return chat/work, confirmed absent, or unknown. Read errors are never absence."""
    try:
        value = page.evaluate(JS_READ_MODE)
        return value if value in ("chat", "work", "absent", "unknown") else "unknown"
    except Exception:
        return "unknown"


def mode_probe_value(mode: str) -> str:
    """Environment status distinguishes confirmed absence from observation failure."""
    return "none" if mode == "absent" else mode if mode in ("chat", "work") else "unknown"


def ensure_chat_mode(page) -> tuple[bool, str]:
    """Pro가 존재하는 Chat 모드로 보정한다.
    확인된 컨트롤 부재만 통과하며, invalid/unknown은 실패한다."""
    mode = read_mode(page)
    if mode == "chat":
        return True, "chat"
    if mode == "absent":
        return True, "absent"
    if mode != "work":
        return False, "unknown"
    try:
        if not page.evaluate(JS_CLICK_MODE, "Chat"):
            return False, "unknown"
    except Exception:
        return False, "unknown"
    for _ in range(10):
        time.sleep(0.5)
        now = read_mode(page)
        if now == "chat":
            return True, "chat"
        if now in ("unknown", "absent"):
            return False, now
    return False, read_mode(page)


def read_model_pills(page) -> list[str]:
    out = []
    for el in page.query_selector_all('button[data-codex-intelligence-trigger], button.__composer-pill'):
        try:
            t = (el.inner_text() or "").strip()
            if t:
                out.append(t)
        except Exception:
            continue
    return out


def _close_switcher(page) -> None:
    """스위처 팝오버 닫기. 새 UI에선 서브메뉴가 열려 있으면 Escape 1회는 서브메뉴만
    닫으므로, 팝오버가 사라질 때까지 최대 3회 누른다.
    주의: 메뉴가 이미 닫혀 있으면 Escape를 누르지 않는다 — 응답 생성 중에 페이지에
    Escape가 가면 '응답 생성을 중지할까요?' 다이얼로그가 떠버린다(2026-08-18 실측)."""
    try:
        for _ in range(3):
            if not page.query_selector(f'{INTELLIGENCE_PICKER_SELECTOR}, [role="menu"][data-state="open"], [role="menu"][data-radix-menu-content]'):
                break
            page.keyboard.press("Escape")
            time.sleep(0.3)
    except Exception:
        pass


def _open_switcher_raw(page) -> bool:
    """pill 클릭으로 팝오버만 연다(뷰 전환 없음). 이미 열려 있으면 그대로 True."""
    try:
        if page.query_selector(f'{INTELLIGENCE_PICKER_SELECTOR}, [role="menu"][data-radix-menu-content]'):
            return True
    except Exception:
        pass
    for sel in MODEL_SWITCHER_SELECTORS:
        try:
            el = page.query_selector(sel)
            if el:
                el.click()
                time.sleep(1.2)
                return True
        except Exception:
            continue
    return False


SUPPORTED_EFFORT_SLIDER_SELECTOR = (
    '[data-reasoning-slider] [role="slider"], '
    '[data-model-reasoning-effort-slider] [role="slider"], '
    '[data-testid="composer-intelligence-picker-content"] [role="slider"]')
EFFORT_BY_INDEX = {0: "instant", 1: "standard", 2: "high", 3: "extra_high", 4: "pro"}
NEUTRAL_EFFORT_CAPTIONS = {"추론 수준", "추론 강도", "reasoning level", "reasoning effort"}


class SafeSelectionFailure(Exception):
    """An allowlisted, preformatted diagnostic; never contains raw page text."""


def _slider_value(page, scope=None) -> tuple[int, int, int] | None:
    """Return validated (min, current, max) for the unique slider inside scope."""
    if scope is None:
        return None
    try:
        sliders = scope.query_selector_all(SUPPORTED_EFFORT_SLIDER_SELECTOR)
        if len(sliders) != 1:
            return None
        values = [sliders[0].get_attribute(k) for k in ("aria-valuemin", "aria-valuenow", "aria-valuemax")]
        if any(v is None or not re.fullmatch("[0-4]", v) for v in values):
            return None
        minimum, current, maximum = map(int, values)
        if minimum != 0 or not minimum <= current <= maximum <= 4:
            return None
        return minimum, current, maximum
    except Exception:
        return None


def _set_effort_slider(page, target_idx: int, scope=None) -> bool:
    """새 UI(2026-08): 추론단계 슬라이더를 target_idx로 이동.
    서브메뉴 radio는 슬라이더 파티클 애니메이션의 상시 리렌더로 클릭이 detach 실패하므로
    (일반/force/좌표 클릭 전부 무효 실측), 유일하게 안정적인 경로는
    SliderControl 프로그램 focus + ArrowLeft/ArrowRight 키 입력이다."""
    try:
        for _attempt in range(2):
            position = _slider_value(page, scope)
            if position is None:
                return False
            _mn, cur, mx = position
            if cur == target_idx:
                return True
            ok = page.evaluate("""(scope) => {
              const root = scope || document;
              const ss = root.querySelectorAll('[data-reasoning-slider] [role="slider"], [data-model-reasoning-effort-slider] [role="slider"], [data-testid="composer-intelligence-picker-content"] [role="slider"]');
              if (ss.length !== 1) return false;
              const c = ss[0].closest('[data-reasoning-slider]') || ss[0].closest('[data-model-reasoning-effort-slider]')?.closest('[role="menuitem"]');
              if (!c || (scope && !scope.contains(c))) return false;
              c.focus();
              return document.activeElement === c;
            }""", scope)
            if not ok:
                return False
            key = "ArrowRight" if target_idx > cur else "ArrowLeft"
            for _ in range(abs(target_idx - cur)):
                page.keyboard.press(key)
                time.sleep(0.4)
        position = _slider_value(page, scope)
        return position is not None and position[1] == target_idx
    except Exception:
        return False


# 새 UI 슬라이더 인덱스 폴백 맵(서브메뉴 라벨을 못 읽었을 때만 사용).
EFFORT_SLIDER_FALLBACK = {"즉시": 0, "중간": 1, "높음": 2, "매우 높음": 3, "pro": 4,
                          "instant": 0, "standard": 1, "high": 2, "extended": 3}


EFFORT_ALIASES = {"pro": "pro", "high": "high", "높음": "high",
                  "extra high": "extra_high", "extended": "extra_high", "매우 높음": "extra_high",
                  "medium": "standard", "standard": "standard", "중간": "standard",
                  "instant": "instant", "즉시": "instant"}


def canonical_effort(text: str) -> str | None:
    return EFFORT_ALIASES.get(normalize(text).casefold())


def _label_effort(trigger, pill: str, model: str, current: bool,
                  *, include_attribute: bool = True) -> tuple[str | None, bool]:
    """Return canonical label effort evidence and whether a visible effort surface is invalid.

    A validated linked slider is authoritative for the current picker. Its trigger attribute
    remains metadata and is not treated as competing effort evidence.
    """
    candidates = []
    attr = trigger.get_attribute("data-selected-reasoning-effort") if include_attribute else None
    if attr:
        mapped = canonical_effort(attr)
        if mapped is None:
            return None, True
        candidates.append(mapped)
    label = normalize(pill)
    mapped = canonical_effort(label)
    if mapped is not None:
        candidates.append(mapped)
    elif label.casefold() in {x.casefold() for x in NEUTRAL_EFFORT_CAPTIONS}:
        pass
    elif not current and model and label.casefold().startswith((model + " ").casefold()):
        suffix = label[len(model):].strip()
        mapped = canonical_effort(suffix)
        if mapped is None:
            return None, True
        candidates.append(mapped)
    elif label:
        return None, True
    if len(set(candidates)) > 1:
        return candidates[-1], True
    return (candidates[0] if candidates else None), False


def selection_state(page) -> dict:
    triggers = [e for e in page.query_selector_all(_selector_union(MODEL_SWITCHER_SELECTORS)) if e.is_visible()]
    if len(triggers) != 1:
        raise RuntimeError("활성 모델 trigger가 유일하지 않음")
    trigger = triggers[0]
    tid = trigger.get_attribute("id")
    controls = trigger.get_attribute("aria-controls")
    menus = [m for m in page.query_selector_all('[role="menu"], ' + INTELLIGENCE_PICKER_SELECTOR)
             if m.is_visible() and ((tid and tid in (m.get_attribute("aria-labelledby") or "").split())
                                    or (controls and m.get_attribute("id") == controls))]
    if len(menus) != 1:
        raise RuntimeError("trigger에 연결된 유일 메뉴 미확인")
    menu = menus[0]
    current = trigger.get_attribute("data-codex-intelligence-trigger") == "true"
    radios = menu.query_selector_all('[role="menuitemradio"]')
    if current:
        selected = [r for r in radios if r.get_attribute("aria-checked") == "true"
                    and r.get_attribute("data-model-selected") == "true"]
        if len([r for r in radios if r.get_attribute("aria-checked") == "true"]) != 1:
            raise RuntimeError("모델 선택 표시 모순")
    else:
        selected = [r for r in radios if r.get_attribute("aria-checked") == "true"
                    and canonical_effort(r.inner_text()) is None]
    if len(selected) != 1:
        raise RuntimeError("선택 모델 유일성 미확인")
    model = selected[0].inner_text().strip().splitlines()[0]
    pill = trigger.inner_text().strip()
    sliders = menu.query_selector_all(SUPPORTED_EFFORT_SLIDER_SELECTOR)
    if len(sliders) > 1:
        raise RuntimeError("linked picker slider is not unique")
    pos = _slider_value(page, menu) if sliders else None
    if sliders and pos is None:
        raise RuntimeError("linked picker slider range is unsupported")
    if current and not sliders:
        raise RuntimeError("current picker slider is missing")
    label_effort, label_invalid = _label_effort(
        trigger, pill, model, current, include_attribute=not bool(pos))
    if label_invalid:
        detail = (f"slider current={pos[1]}/{pos[2]}, effort={EFFORT_BY_INDEX[pos[1]]}; "
                  f"label effort={label_effort or 'unknown'}" if pos else "effort label is unsupported")
        raise SafeSelectionFailure(f"추론단계 표기가 일치하지 않아 차단했습니다 ({detail}).")
    effort = EFFORT_BY_INDEX[pos[1]] if pos else label_effort
    if pos and label_effort and label_effort != effort:
        raise SafeSelectionFailure(
            f"추론단계 표기가 일치하지 않아 차단했습니다 (slider current={pos[1]}/{pos[2]}, "
            f"effort={effort}, label effort={label_effort}).")
    if not pos and not current:
        checked = [canonical_effort(r.inner_text()) for r in radios if r.get_attribute("aria-checked") == "true"
                   and canonical_effort(r.inner_text())]
        if len(checked) != 1 or (effort and effort != checked[0]):
            raise RuntimeError("effort 선택 표시 모순")
        effort = checked[0]
    toggle = menu.query_selector('[data-model-picker-view-toggle]')
    return {"observed_selection": model, "actual_display": toggle.inner_text().strip() if toggle else pill,
            "effort": effort, "slider": pos, "current": current, "menu": menu,
            "actual_model_verification": "selected_radio", "trigger_label": pill}


def select_model(page, want: str, require_model: str | None = None) -> tuple[bool, dict | None]:
    target = canonical_effort(want)
    try:
        if target is None or not _open_switcher_raw(page):
            raise RuntimeError("요청 effort/모델 메뉴 미확인")
        before = selection_state(page)
        position = before["slider"]
        if position:
            index = {"instant": 0, "standard": 1, "high": 2, "extra_high": 3, "pro": 4}[target]
            if target == "pro" and position[2] < 4:
                current_effort = EFFORT_BY_INDEX[position[1]]
                raise SafeSelectionFailure(
                    f"Pro를 사용할 수 없어 선택을 변경하지 않았습니다 (slider max={position[2]}, "
                    f"current effort={current_effort}).")
            if not position[0] <= index <= position[2] or not _set_effort_slider(page, index, before["menu"]):
                raise RuntimeError("slider 이동 확인 실패")
        else:
            candidates = [r for r in before["menu"].query_selector_all('[role="menuitemradio"]')
                          if canonical_effort(r.inner_text()) == target]
            if len(candidates) != 1:
                raise RuntimeError("effort 항목 유일성 미확인")
            candidates[0].click()
            if not _open_switcher_raw(page):
                raise RuntimeError("선택 후 메뉴 재확인 실패")
        deadline = time.monotonic() + 2
        previous = None
        while True:
            after = selection_state(page)
            model_ok = (after["observed_selection"] == before["observed_selection"]
                        and (not require_model or require_model.casefold() in after["observed_selection"].casefold()))
            slider_ok = not position or (after["slider"] is not None and after["slider"][1] == index)
            public = {k: v for k, v in after.items() if k not in ("menu", "current")}
            if model_ok and slider_ok and after["effort"] == target:
                if previous == public:
                    print(f"  ✓ 모델/추론단계 사전검증 완료 (effort={target})", flush=True)
                    return True, public
                previous = public
            else:
                previous = None
            if time.monotonic() >= deadline:
                raise RuntimeError("선택 후 모델/slider/effort 불일치")
            time.sleep(0.25)
    except SafeSelectionFailure as exc:
        print(f"  ❌ {exc} 전송을 중단했습니다.", flush=True)
        return False, None
    except Exception:
        print("  ❌ 모델/추론단계 상태를 확인할 수 없어 전송을 중단했습니다.", flush=True)
        return False, None
    finally:
        _close_switcher(page)


# ---- 첨부 / 입력 / 전송 ----
# `current`는 2026-10-01 visible accessibility UI에서 확인한 역할/이름만 사용한다.
# legacy/unknown UI는 근거가 없어 계속 fail-closed. Enter 폴백은 활성화하지 않는다.
_DISPATCH_ADAPTERS = {
    "current": {
        "click": True,
        "enter": False,
        "accessible_controls": True,
        "evidence": "visible current composer exposes one enabled send button; atomic guarded click only",
    },
}
_ATTACHMENT_ADAPTERS = {
    "current": {
        "strategy": "accessible_controls",
        "evidence": "visible current composer exposes exact filename button, matching remove button, upload status text, and send readiness",
    },
}
_SAFE_ATTACHMENT_REASONS = frozenset({
    "unsupported", "composer_scope_ambiguous", "baseline_attachment_present",
    "baseline_upload_pending", "baseline_filename_collision",
    "file_input_missing_or_ambiguous", "upload_unconfirmed",
    "add_files_control_missing_or_ambiguous",
    "upload_action_missing", "upload_action_ambiguous", "upload_action_unowned",
    "file_chooser_not_opened",
    "composer_changed_during_upload", "upload_readiness_unconfirmed",
    "attachment_evidence_failed", "ambiguous_baseline", "no_file_input",
    "ambiguous_file_input",
})


def active_composer(page):
    editors = [e for e in page.query_selector_all(_selector_union(INPUT_SELECTORS))
               if e.is_visible() and e.is_enabled()]
    if len(editors) != 1:
        raise RuntimeError("활성 composer가 유일하지 않음")
    return editors[0]


def composer_guard(page, editor, expected=None):
    return page.evaluate(r"""({editor, selector, expected}) => {
        const visible = e => e.isConnected && e.getClientRects().length > 0 &&
            getComputedStyle(e).visibility !== 'hidden' && !e.matches(':disabled') &&
            e.getAttribute('aria-disabled') !== 'true';
        const editors = [...document.querySelectorAll(selector)].filter(visible);
        const norm = s => s.replace(/\s+/gu, ' ').trim();
        return editors.length === 1 && editors[0] === editor && editor.isContentEditable &&
            (expected === null || norm(editor.innerText) === expected);
    }""", {"editor": editor, "selector": _selector_union(INPUT_SELECTORS),
             "expected": normalize(expected) if expected is not None else None})


_REMOVE_CONTROL_RE = re.compile(r"^(?:(?:remove|제거)\s+\S.*|.+\s(?:remove|제거))$", re.I)
_ANY_UPLOAD_STATUS_RE = re.compile(
    r"^(?:.+\s+(?:업로드 중|uploading(?:\.{3}|…)?)|uploading(?:\.{3}|…)?\s+.+)$", re.I)


def _current_composer_scope(page, editor):
    """Return the unique form/presentation Locator containing this active editor."""
    for selector in ("form", '[role="presentation"]'):
        candidates = page.locator(selector)
        matches = []
        for index in range(candidates.count()):
            candidate = candidates.nth(index)
            if candidate.evaluate("(scope, editor) => scope.contains(editor)", editor):
                matches.append(candidate)
        if matches:
            return matches[0] if len(matches) == 1 else None
    return None


def _visible_locator_count(locator) -> int:
    count = locator.count()
    return sum(1 for index in range(count) if locator.nth(index).is_visible())


def _progress_pattern(filename):
    """이 파일의 업로드 진행 표시 정규식. 첨부 확인(Python)과 최종 전송 가드(JS)가 같은 규칙을 쓰도록 한 곳에서
    만든다(독립 리뷰 F4: `uploading… <파일명>`을 앞 단계는 진행 중으로, 최종 가드는 진행 없음으로 해석했다)."""
    escaped = re.escape(filename)
    dots = r"(?:\.{3}|…)"  # rf-문자열 안에서 직접 쓰면 {3}이 f-string 값(3)으로 바뀌므로 일반 문자열로 둔다
    suffix = rf"(?:업로드 중|uploading{dots}?|uploading)"
    return re.compile(rf"^(?:{escaped}\s+{suffix}|uploading{dots}?\s+{escaped})$", re.I)


def _attachment_progress_locator(scope, filename=None):
    pattern = _progress_pattern(filename) if filename else _ANY_UPLOAD_STATUS_RE
    return scope.get_by_text(pattern, exact=True)


def _attachment_remove_locator(scope, filename=None):
    if filename:
        escaped = re.escape(filename)
        pattern = re.compile(
            rf"^(?:{escaped}\s+(?:remove|제거)|(?:remove|제거)\s+{escaped})$", re.I)
    else:
        pattern = _REMOVE_CONTROL_RE
    return scope.get_by_role("button", name=pattern)


_CURRENT_SEND_NAME_RE = re.compile(r"^(?:send|보내기|프롬프트 보내기)$", re.I)
_CURRENT_ADD_FILES_RE = re.compile(r"^(?:파일 등 추가|add files|attach files|add photos and files)$", re.I)
_CURRENT_UPLOAD_ACTION_RE = re.compile(
    r"^(?:사진 및 파일 추가 컴퓨터에서 업로드|사진 및 파일 업로드|파일 업로드|"
    r"컴퓨터에서 파일 업로드|컴퓨터에서 업로드|기기에서 파일 업로드|기기에서 업로드|"
    r"파일 선택|컴퓨터에서 파일 선택|기기에서 파일 선택|"
    r"Upload files?|Upload from computer|Choose files? from your computer|"
    r"Select files?|Browse files?)$", re.I)


def _current_send_locator(scope):
    return scope.get_by_role("button", name=_CURRENT_SEND_NAME_RE, exact=True)


def _current_send_ready(scope) -> bool:
    buttons = _current_send_locator(scope)
    return _visible_locator_count(buttons) == 1 and buttons.is_enabled()


def _current_file_menu_action(page):
    locator = page.get_by_role("button", name=_CURRENT_UPLOAD_ACTION_RE, exact=True)
    actions = [locator.nth(index) for index in range(locator.count())
               if locator.nth(index).is_visible() and locator.nth(index).is_enabled()]
    if len(actions) == 1:
        return actions[0], 1, None
    reason = "upload_action_missing" if not actions else "upload_action_ambiguous"
    return None, len(actions), reason


# 실측(2026-10-01): 메뉴는 BODY 아래 id/role 없는 DIV로 렌더링되고 "+" 버튼과 left가 같다(365=365),
# composer 바로 아래/위에 인접한다(간격 20px). 메뉴 컨테이너 = 항목의 조상 중 보이는 버튼이 2개 이상인 첫 요소.
_MENU_ANCHOR_JS = """(action, plus) => {
    const visible = e => e.isConnected && e.getClientRects().length > 0;
    let menu = action.parentElement;
    while (menu && [...menu.querySelectorAll('button')].filter(visible).length < 2) menu = menu.parentElement;
    if (!menu || menu === document.body || menu === document.documentElement) return false;
    const m = menu.getBoundingClientRect(), p = plus.getBoundingClientRect();
    const gap = m.top >= p.bottom ? m.top - p.bottom : (p.top >= m.bottom ? p.top - m.bottom : -1);
    return Math.abs(m.left - p.left) <= 8 && gap >= 0 && gap <= 64;
}"""


def _upload_menu_anchored(action, add_button) -> bool:
    try:
        return bool(action.evaluate(_MENU_ANCHOR_JS, add_button.element_handle()))
    except Exception:
        return False


def _choose_current_file(page, scope, path: Path) -> str | None:
    """Choose the pack through the current accessible add/upload controls.

    Returns a safe failure reason, or None once the native chooser accepted this exact file.
    Visible composer identity/readiness is verified separately after selection.
    """
    # 업로드 항목은 composer 폼 '밖'의 팝오버 버튼이라 조상 범위로는 소유권을 확인할 수 없다(실측 2026-10-01).
    # 대신 이 composer의 "파일 등 추가" 버튼이 메뉴를 연 상태(aria-expanded=true)일 때만 그 항목을 인정한다.
    # 페이지의 다른 업로더가 이미 열려 있거나 이름이 같은 버튼이 있어도 파일을 전달하지 않는다.
    add_buttons = scope.get_by_role("button", name=_CURRENT_ADD_FILES_RE, exact=True)
    if _visible_locator_count(add_buttons) != 1:
        return "add_files_control_missing_or_ambiguous"
    add_button = add_buttons.first

    def _add_menu_open() -> bool:
        try:
            return add_button.get_attribute("aria-expanded") == "true"
        except Exception:
            return False

    chooser = None
    if not _add_menu_open():
        try:
            with page.expect_file_chooser(timeout=1200) as info:
                add_button.click()
            chooser = info.value
        except PlaywrightTimeoutError:
            # Current ChatGPT exposes "사진 및 파일 추가 컴퓨터에서 업로드" as a standalone button.
            pass
    if chooser is None:
        if not _add_menu_open():
            return "upload_action_unowned"
        action, _action_count, action_error = _current_file_menu_action(page)
        if action is None:
            return action_error
        # aria-expanded만으로는 '클릭할 항목'이 이 메뉴 소속임을 보장하지 못한다(독립 리뷰 F1). 실측상 메뉴에는
        # aria-controls/id가 없으므로, 항목이 속한 메뉴가 이 composer의 "+" 버튼에 정렬·인접해 있을 때만 인정한다.
        if not _upload_menu_anchored(action, add_button):
            return "upload_action_unowned"

    if chooser is None:
        try:
            with page.expect_file_chooser(timeout=5000) as info:
                action.click()
            chooser = info.value
        except PlaywrightTimeoutError:
            return "file_chooser_not_opened"

    chooser.set_files(str(path))
    # The chooser is bound to the exact path above. Some composer change handlers
    # immediately clear/recreate their transient file input, so reading chooser.element
    # after set_files can see an empty FileList even though the UI accepted the upload.
    # _attach_file_current verifies the exact visible filename, matching remove control,
    # finished upload state and enabled Send twice; dispatch rechecks them atomically.
    return None


def _attach_file_current(page, path: Path) -> dict:
    result = {"state": "not_attempted", "fallback_allowed": False, "reason": "unsupported"}
    try:
        editor = active_composer(page)
        scope = _current_composer_scope(page, editor)
        if scope is None or not composer_guard(page, editor):
            result["reason"] = "composer_scope_ambiguous"
            return result

        # Do not send an existing draft attachment along with the review pack.
        if _visible_locator_count(_attachment_remove_locator(scope)):
            result["reason"] = "baseline_attachment_present"
            print("  ❌ composer에 기존 파일 첨부가 있습니다. 기존 첨부를 확인·제거한 뒤 다시 실행하세요.", flush=True)
            return result
        if _visible_locator_count(_attachment_progress_locator(scope)):
            result["reason"] = "baseline_upload_pending"
            print("  ❌ composer에 완료되지 않은 파일 업로드가 있습니다. 업로드 상태를 정리한 뒤 다시 실행하세요.", flush=True)
            return result

        file_buttons = scope.get_by_role("button", name=path.name, exact=True)
        if _visible_locator_count(file_buttons):
            result["reason"] = "baseline_filename_collision"
            return result
        result.update(state="attempted_unconfirmed", reason="upload_unconfirmed")
        # Some ChatGPT builds create the file input only after opening the visible add/upload menu.
        failure = _choose_current_file(page, scope, path)
        if failure:
            result["reason"] = failure
            return result

        previous_ready = False
        for _ in range(40):
            if not composer_guard(page, editor):
                result["reason"] = "composer_changed_during_upload"
                return result
            file_buttons = scope.get_by_role("button", name=path.name, exact=True)
            remove_buttons = _attachment_remove_locator(scope, path.name)
            progress = _attachment_progress_locator(scope, path.name)
            file_count = _visible_locator_count(file_buttons)
            remove_count = _visible_locator_count(remove_buttons)
            pending_count = _visible_locator_count(progress)
            send_ready = _current_send_ready(scope)
            signature = (file_count, remove_count, pending_count, send_ready)
            ready = signature == (1, 1, 0, True)
            if ready and previous_ready:
                result.update(state="confirmed", reason="new_accessible_attachment_ready",
                              identity=path.name, filename=path.name)
                print("  ✓ 새 파일명 첨부와 업로드 준비 완료를 확인했습니다.", flush=True)
                return result
            previous_ready = ready
            time.sleep(1)
        result["reason"] = "upload_readiness_unconfirmed"
        return result
    except Exception:
        result["reason"] = "attachment_evidence_failed"
        return result
    finally:
        if result["state"] == "attempted_unconfirmed":
            print(f"  ❌ '{path.name}' 첨부 준비를 확인하지 못했습니다. 파일명이 composer에 남아 있으면 '{path.name} 제거' 버튼으로 정리한 뒤 재시도하세요.", flush=True)


def attachment_snapshot(scope, editor, adapter):
    return scope.evaluate("""(scope, {editor, adapter}) => {
        const visible = e => e.isConnected && e.getClientRects().length > 0 &&
            getComputedStyle(e).visibility !== 'hidden';
        if (!editor.isConnected || !scope.contains(editor)) throw Error('composer changed');
        const chips = [...scope.querySelectorAll(adapter.chip)].filter(visible);
        if (chips.some(e => e.contains(editor) || editor.contains(e))) throw Error('bad chip scope');
        const pending = new Set();
        for (const progress of [...scope.querySelectorAll(adapter.progress)].filter(visible)) {
            const owners = chips.filter(chip => chip.contains(progress));
            if (owners.length !== 1 || !owners[0].getAttribute(adapter.identity))
                throw Error('unidentified or ambiguous upload in progress');
            pending.add(owners[0]);
        }
        return chips.map(e => ({id:e.getAttribute(adapter.identity),
            names: adapter.names.map(a => a === 'text' ? e.innerText.trim() : e.getAttribute(a)),
            ready:e.matches(adapter.ready) && !pending.has(e)}));
    }""", {"editor": editor, "adapter": adapter})


def attach_file(page, path: Path) -> dict:
    result = {"state": "not_attempted", "fallback_allowed": False, "reason": "unsupported"}
    try:
        adapter = _ATTACHMENT_ADAPTERS.get(ui_adapter(page))
        if not adapter or not adapter.get("evidence"):
            print("  첨부 unsupported — 새 파일 identity/readiness의 실제 UI 근거 없음", flush=True)
            return result
        if adapter.get("strategy") == "accessible_controls":
            return _attach_file_current(page, path)
        editor = active_composer(page)
        scope = editor.evaluate_handle("el => el.closest('form') || el.closest('[role=presentation]')").as_element()
        if scope is None:
            return result
        baseline = attachment_snapshot(scope, editor, adapter)
        ids = [c["id"] for c in baseline]
        if any(not i for i in ids) or len(set(ids)) != len(ids):
            result["reason"] = "ambiguous_baseline"
            return result
        if any(not c["ready"] for c in baseline):
            result["reason"] = "baseline_upload_pending"
            return result
        inputs = scope.query_selector_all(FILE_INPUT_SELECTOR)
        if not inputs:
            result.update(fallback_allowed=not baseline, reason="no_file_input")
            return result
        if len(inputs) != 1:
            result["reason"] = "ambiguous_file_input"
            return result
        result.update(state="attempted_unconfirmed", reason="upload_unconfirmed")
        inputs[0].set_input_files(str(path))
        # Assignment is correlation evidence only, never readiness evidence.
        metadata = inputs[0].evaluate("e => [...e.files].map(f => ({name:f.name,size:f.size}))")
        if metadata != [{"name": path.name, "size": path.stat().st_size}]:
            return result
        for _ in range(40):
            if not composer_guard(page, editor):
                return result
            chips = attachment_snapshot(scope, editor, adapter)
            current_ids = [c["id"] for c in chips]
            if any(not i for i in current_ids) or len(set(current_ids)) != len(current_ids):
                return result
            fresh = [c for c in chips if c["id"] not in ids]
            if len(fresh) == 1 and path.name in fresh[0]["names"] and fresh[0]["ready"]:
                result.update(state="confirmed", reason="new_attachment_ready", identity=fresh[0]["id"])
                print("  ✓ 이번 파일의 새 첨부 identity/준비 완료 확인", flush=True)
                return result
            time.sleep(1)
        return result
    except Exception:
        result["reason"] = "attachment_evidence_failed"
        return result


def build_paste_fallback(prompt: str, pack_path: Path) -> str | None:
    """첨부 실패 시 pack을 프롬프트에 인라인으로 붙여 보낼 메시지를 구성.
    크기 상한 초과면 None(호출자가 조용히 자르지 않고 fail-closed) — 잘린 컨텍스트 전송 방지."""
    try:
        body = pack_path.read_text(encoding="utf-8", errors="strict")
    except OSError:
        return None
    if len(body) > PASTE_FALLBACK_MAX_CHARS:
        return None
    return f'{prompt}\n\n<repomix_pack file="{pack_path.name}">\n{body}\n</repomix_pack>'


SEND_BTN_SELECTORS = [
    'button[data-testid="send-button"]',
    'button[data-testid="composer-send-button"]',
    'button[aria-label*="send" i]',
    'button[aria-label*="보내기" i]',
    'button[aria-label*="프롬프트 보내기" i]',
]


def put_text(page, message: str, composer=None):
    editor = composer if composer is not None else active_composer(page)
    if not composer_guard(page, editor):
        raise RuntimeError("입력 composer 변경/미확인")
    editor.fill(message)
    return editor


def read_composer_text(page, composer=None) -> str:
    editor = composer if composer is not None else active_composer(page)
    if not composer_guard(page, editor):
        raise RuntimeError("읽기 composer 변경/미확인")
    return editor.inner_text()


def composer_has_prompt(page, prompt: str, composer=None) -> bool:
    try:
        return normalize(read_composer_text(page, composer)) == normalize(prompt)
    except Exception:
        return False


def clear_composer(page, composer=None):
    editor = composer if composer is not None else active_composer(page)
    if not composer_guard(page, editor):
        raise RuntimeError("지우기 composer 변경/미확인")
    editor.fill("")


def _guarded_dispatch(page, editor, expected, button, mode, attachment=None, accessible_send=False):
    # One synchronous JS task: no await/focus/scroll/API action after the guard.
    return page.evaluate(r"""({editor, expected, button, mode, selector, sendSelector, attachment, accessibleSend}) => {
        const visible = e => e && e.isConnected && e.getClientRects().length > 0 &&
            getComputedStyle(e).visibility !== 'hidden' && !e.matches(':disabled') &&
            e.getAttribute('aria-disabled') !== 'true';
        const editors = [...document.querySelectorAll(selector)].filter(visible);
        const norm = s => s.replace(/\s+/gu, ' ').trim();
        if (editors.length !== 1 || editors[0] !== editor || !editor.isContentEditable ||
            norm(editor.innerText) !== expected) return false;
        const scope = editor.closest('form') || editor.closest('[role="presentation"]');
        const labels = e => {
            const result = [e.getAttribute('aria-label'), e.getAttribute('title'), e.innerText, e.textContent];
            const labelledBy = e.getAttribute('aria-labelledby');
            if (labelledBy) for (const id of labelledBy.split(/\s+/)) {
                const ref = document.getElementById(id);
                if (ref) result.push(ref.innerText || ref.textContent || '');
            }
            return result.map(x => norm(x || '')).filter(Boolean);
        };
        if ((attachment || accessibleSend) && (!scope || !scope.contains(editor))) return false;
        if (attachment) {
            const filename = norm(attachment.filename || attachment.identity || '');
            if (!filename) return false;
            const candidates = [...scope.querySelectorAll('button')].filter(visible);
            const fileButtons = candidates.filter(b => labels(b).includes(filename));
            const isRemove = s => {
                const n = norm(s).toLowerCase(), f = filename.toLowerCase();
                return n === `${f} 제거` || n === `${f} remove` ||
                    n === `제거 ${f}` || n === `remove ${f}`;
            };
            const removeButtons = candidates.filter(b => labels(b).some(isRemove));
            const allRemoveButtons = candidates.filter(b => labels(b).some(s => /^(?:(?:remove|제거)\s+\S.*|.+\s(?:remove|제거))$/i.test(s)));
            if (fileButtons.length !== 1 || removeButtons.length !== 1 || allRemoveButtons.length !== 1)
                return false;
            if ([...scope.querySelectorAll('[role="progressbar"]')].some(visible)) return false;
            const progressRe = new RegExp(attachment.progressPattern, 'i');
            // 실측(2026-10-01): 업로드 중에는 role=progressbar의 aria-label이 "<파일명> 업로드 중"이기도 하다.
            if ([...scope.querySelectorAll('*')].some(e => {
                if (!visible(e) || editor.contains(e) || e === editor) return false;
                return progressRe.test(norm(e.innerText || e.textContent || '')) ||
                    progressRe.test(norm(e.getAttribute('aria-label') || ''));
            })) return false;
        }
        if (mode === 'click') {
            const buttons = accessibleSend
                ? [...scope.querySelectorAll('button')].filter(visible).filter(b =>
                    labels(b).some(s => /^(?:send|보내기|프롬프트 보내기)$/i.test(s)))
                : [...document.querySelectorAll(sendSelector)].filter(visible);
            if (buttons.length !== 1 || buttons[0] !== button) return false;
            button.click();
        } else {
            if (document.activeElement !== editor) return false;
            editor.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter',code:'Enter',bubbles:true,cancelable:true}));
        }
        return true;
    }""", {"editor": editor, "expected": normalize(expected), "button": button,
             "mode": mode, "selector": _selector_union(INPUT_SELECTORS),
             "sendSelector": _selector_union(SEND_BTN_SELECTORS),
             "attachment": (dict(attachment, progressPattern=_progress_pattern(
                 str(attachment.get("filename") or attachment.get("identity") or "")).pattern)
                 if attachment else attachment),
             "accessibleSend": accessible_send})


DISPATCH_REASONS = {
    "preparation": "전송 준비/검증 실패",
    "checkpoint": "전송 전 checkpoint 저장 실패",
    "unsupported": "전송 adapter unsupported",
    "guard": "전송 직전 composer/본문/첨부/대상 불일치",
    "activation": "전송 활성화 결과 미확정",
}


def click_send(page, expected_prompt, composer_handle, dispatch=None, attachment=None) -> bool:
    # Runtime evidence is separate from the durable SEND_PENDING intent record.
    if dispatch is None:
        dispatch = {}
    dispatch.update(state="NOT_DISPATCHED", reason="preparation")
    adapter = _DISPATCH_ADAPTERS.get(ui_adapter(page))
    if not adapter or not adapter.get("evidence"):
        dispatch["reason"] = "unsupported"
        raise RuntimeError("guarded dispatch unsupported — 실제 UI activation 근거 없음")
    accessible_send = bool(adapter.get("accessible_controls"))
    # Read failures and validation failures never authorize Enter fallback.
    for _ in range(15):
        if accessible_send:
            scope = _current_composer_scope(page, composer_handle)
            if scope is None:
                raise RuntimeError("current composer 범위 미확인")
            controls = _current_send_locator(scope)
            buttons = [controls.nth(i).element_handle() for i in range(controls.count())
                       if controls.nth(i).is_visible() and controls.nth(i).is_enabled()]
        else:
            buttons = [b for b in page.query_selector_all(_selector_union(SEND_BTN_SELECTORS))
                       if b.is_visible() and b.is_enabled()]
        if len(buttons) > 1:
            raise RuntimeError("제출 버튼 모호")
        if buttons:
            if not adapter.get("click"):
                dispatch["reason"] = "unsupported"
                raise RuntimeError("guarded click unsupported")
            button = buttons[0]
            button.click(trial=True)  # actionability/scroll only, no dispatch
            dispatch.update(state="ACTIVATION_UNKNOWN", reason="activation")
            activated = _guarded_dispatch(page, composer_handle, expected_prompt, button, "click",
                                          attachment, accessible_send)
            if activated is False:
                dispatch.update(state="NOT_DISPATCHED", reason="guard")
                raise RuntimeError("전송 직전 composer/본문/첨부/대상 불일치")
            if activated is not True:
                raise RuntimeError("전송 활성화 결과 미확정")
            dispatch["state"] = "ACTIVATED"
            return True  # exceptions/uncertain acknowledgement propagate, no second action
        time.sleep(1)
    if not adapter.get("enter"):
        dispatch["reason"] = "unsupported"
        raise RuntimeError("guarded Enter unsupported")
    composer_handle.focus()
    dispatch.update(state="ACTIVATION_UNKNOWN", reason="activation")
    activated = _guarded_dispatch(page, composer_handle, expected_prompt, None, "enter",
                                  attachment, accessible_send)
    if activated is False:
        dispatch.update(state="NOT_DISPATCHED", reason="guard")
        raise RuntimeError("Enter 직전 composer/본문/첨부/포커스 불일치")
    if activated is not True:
        raise RuntimeError("전송 활성화 결과 미확정")
    dispatch["state"] = "ACTIVATED"
    return True


def click_answer_now(page) -> bool:
    """리즈닝 중 '지금 답변 받기'를 눌러 강제 답변.
    실측 2026-07-19(cot v5 UI): 버튼은 우측 flyout이 아니라 본문 리즈닝 고정행
    (div[data-testid="cot-v5-pinned-row"]) 안의 button. 이 행은 TransitionGroup
    애니메이션 속이라 Playwright 안정성 판정이 타임아웃될 수 있어 force 클릭 폴백을 둔다.
    구 UI(우측 flyout) 대비 텍스트 매칭 경로는 폴백으로 유지.
    칩 매칭은 '생각 중'으로 좁힌다 — 프롬프트 본문의 '추론' 등과 오매칭 방지."""
    # 1) 신 UI: 고정행 셀렉터 직행(스크롤 조작 불필요 — 요소 단위 scroll_into_view만)
    try:
        row = page.query_selector(ANSWER_NOW_ROW_SELECTOR)
        if row:
            btns = [b for b in row.query_selector_all("button") if b.is_visible()]
            target = next((b for b in btns if ANSWER_NOW_TEXT_RE.search(b.inner_text() or "")),
                          btns[0] if len(btns) == 1 else None)
            if target:
                try:
                    target.scroll_into_view_if_needed(timeout=2000)
                except Exception:
                    pass
                try:
                    target.click(timeout=2500)
                    return True
                except Exception:
                    try:
                        target.click(force=True)  # 애니메이션 중 안정성 판정 실패 대비
                        return True
                    except Exception:
                        pass  # 셀렉터 경로 실패 → 아래 텍스트 매칭 폴백
    except Exception:
        pass

    # 2) 구 UI 폴백: 텍스트 매칭(+ 리즈닝 칩 열기)
    answer_pats = [("지금 답변 받기", True), ("지금 답변받기", True),
                   ("답변 받기", False), ("Get answer", False), ("answer now", False)]
    chip_re = re.compile(r"생각\s*중|Thinking", re.I)

    def scroll_panels_top():
        try:
            page.evaluate("() => { for (const el of document.querySelectorAll('*')) "
                          "{ if (el.scrollHeight > el.clientHeight + 20) el.scrollTop = 0; } }")
        except Exception:
            pass

    def try_answer() -> bool:
        scroll_panels_top()
        for txt, exact in answer_pats:
            try:
                loc = page.get_by_text(txt, exact=exact)
                if loc.count() > 0:
                    try:
                        loc.first.scroll_into_view_if_needed(timeout=2000)
                    except Exception:
                        pass
                    try:
                        loc.first.click(timeout=2500)
                    except Exception:
                        loc.first.click(force=True, timeout=2500)  # 애니메이션 안정성 판정 실패 대비
                    return True
            except Exception:
                continue
        return False

    if try_answer():
        return True
    # 리즈닝 칩(좁은 매칭)을 눌러 패널을 연 뒤 재시도
    try:
        chip = page.get_by_text(chip_re)
        if chip.count() > 0:
            chip.first.click(timeout=2500)
            time.sleep(1.2)
    except Exception:
        pass
    return try_answer()


def secure_create(path: Path):
    """새 파일만 만든다(기존 경로를 따라가거나 자르지 않음). 권한 0600, 가능하면 O_NOFOLLOW(Windows는 무시)."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o600)
    return os.fdopen(fd, "wb")


def persist_binding(path: Path, binding: dict) -> None:
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with secure_create(temp) as out:
        out.write(json.dumps(binding, ensure_ascii=False, indent=2).encode())
        out.flush()
        os.fsync(out.fileno())
    os.replace(temp, path)


def _closest(node, selector):
    return node.evaluate_handle("(n, sel) => n.closest(sel)", selector).as_element()


def replies_for_user(page, user):
    if ui_adapter(page) == "current":
        scope = _closest(user, '[data-turn-key]')
        if scope is None:
            raise RuntimeError("user 턴 경계 미확인")
        users = canonical_message_nodes(scope.query_selector_all(_selector_union(USER_MSG_SELECTORS)), True)
        if len(users) != 1 or node_ids(users[0]) != node_ids(user):
            raise RuntimeError("공유 턴 user 모호")
        return canonical_message_nodes(scope.query_selector_all(_selector_union(ASSISTANT_MSG_SELECTORS)), True)
    replies = []
    for assistant in message_nodes(page, "assistant"):
        preceding = assistant.evaluate_handle('''a => {
            const users = [...document.querySelectorAll('[data-message-author-role="user"]')];
            return users.filter(u => !!(a.compareDocumentPosition(u) & Node.DOCUMENT_POSITION_PRECEDING)).pop() || null;
        }''').as_element()
        if preceding is not None and node_ids(preceding) == node_ids(user):
            replies.append(assistant)
    return replies


def tail_confirmed(page) -> bool:
    return bool(page.evaluate('''() => {
        const us = [...document.querySelectorAll('[data-chatgpt-search-unit-key$=":user"], [data-content-search-unit-key$=":user"], [data-message-author-role="user"]')];
        let n = us.at(-1);
        if (!n) return false;
        for (let p=n.parentElement; p; p=p.parentElement) {
            if (p.scrollHeight > p.clientHeight + 8 && /auto|scroll/.test(getComputedStyle(p).overflowY)) {
                // 실측(2026-10-01): 현재 ChatGPT 스레드는 flex-direction:column-reverse 스크롤러라 scrollTop=0이
                // '맨 아래'이고 위로 갈수록 음수다. 이 경우 일반 공식은 이미 맨 아래인데도 "많이 남음"으로 오판한다.
                const cs = getComputedStyle(p);
                if (/flex/.test(cs.display) && cs.flexDirection === 'column-reverse')
                    return p.scrollTop >= -8;
                return p.scrollHeight - p.clientHeight - p.scrollTop <= 8;
            }
        }
        const d=document.scrollingElement;
        return !!d && d.scrollHeight - d.clientHeight - d.scrollTop <= 8;
    }'''))


def _binding_checkpoint(binding, updates, persist):
    candidate = dict(binding, **updates)
    if persist:
        try:
            persist(candidate)
        except Exception as exc:
            binding["checkpoint_error"] = True
            raise RuntimeError("binding checkpoint 저장 실패") from exc
    binding.update(updates)


def bound_reply(page, binding: dict, persist=None):
    users = message_nodes(page, "user")
    expected_user = set(binding.get("sent_user_ids", []))
    if not expected_user:
        if binding.get("original_run_bound"):
            baseline = set(binding["baseline_user_ids"])
            candidates = [u for u in users if node_ids(u) - baseline]
        else:
            if not tail_confirmed(page):
                return None
            candidates = users[-1:]
        if len(candidates) != 1:
            if len(candidates) > 1:
                raise RuntimeError("새 user 후보가 복수입니다")
            return None
        user = candidates[0]
        if binding.get("original_run_bound"):
            shown = user_body_text(user)
            exact_ok = hashlib.sha256(normalize(shown).encode()).hexdigest() == binding.get("sent_text_sha256")
            if not (exact_ok or fingerprint_matches(shown, binding.get("sent_text_fingerprint"))):
                raise RuntimeError("전송 user 본문 불일치")
        scope = _closest(user, '[data-turn-key]') if ui_adapter(page) == "current" else None
        _binding_checkpoint(binding, dict(sent_user_ids=sorted(node_ids(user)), assistant_ids=[],
            turn_key=scope.get_attribute("data-turn-key") if scope else None, phase="USER_BOUND"), persist)
    else:
        candidates = [u for u in users if node_ids(u) == expected_user]
        if len(candidates) != 1:
            return None
        user = candidates[0]
        if not binding.get("original_run_bound") and (not users or node_ids(users[-1]) != expected_user):
            raise RuntimeError("수동 회수 선택 후 user 변경 — 다시 선택하세요")
    replies = replies_for_user(page, user)
    if len(replies) > 1:
        raise RuntimeError("해당 user의 assistant가 유일하지 않음")
    if not replies:
        return None
    node = replies[0]
    ids = node_ids(node)
    if not ids:
        raise RuntimeError("assistant identity 없음")
    expected = set(binding.get("assistant_ids", []))
    if expected and ids != expected:
        raise RuntimeError("회수 대상 assistant 변경 — 자동 재결속 금지")
    if not expected:
        if binding.get("original_run_bound") and not ids - set(binding["baseline_assistant_ids"]):
            raise RuntimeError("기존 assistant를 새 응답으로 사용할 수 없음")
        _binding_checkpoint(binding, dict(assistant_ids=sorted(ids), phase="ASSISTANT_BOUND",
            unit_key=node.get_attribute("data-chatgpt-search-unit-key") or node.get_attribute("data-content-search-unit-key")), persist)
    return node


def conversation_key(url: str | None) -> str | None:
    """대화 정체성은 chatgpt.com의 `/c/<대화ID>`다. 실측(2026-10-01): SPA가 프로젝트 슬러그를 잠깐 떼었다 붙여
    (`/g/g-p-<ID>-<slug>/c/<id>` ↔ `/g/g-p-<ID>/c/<id>`) 문자열 전체 비교는 같은 대화를 '이탈'로 오판한다."""
    try:
        parsed = urllib.parse.urlsplit(url or "")
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.hostname != "chatgpt.com":
        return None
    match = CONV_URL_RE.search(parsed.path)
    return match.group(0).lower() if match else None


def response_snapshot(page, binding: dict, persist=None):
    bound = conversation_key(binding["chat_url"])
    if bound is not None:
        same = conversation_key(current_url(page)) == bound
    else:  # 운영 결속 URL은 항상 /c/<ID>를 갖는다. 그렇지 않은 URL(로컬 fixture 등)은 기존 문자열 비교를 유지한다.
        same = (current_url(page).split("?")[0].rstrip("/") == binding["chat_url"].split("?")[0].rstrip("/"))
    if not same:
        raise RuntimeError("결속 대화 이탈")
    node = bound_reply(page, binding, persist)
    state = streaming_state(page)
    if node is None or state != "absent":
        return None
    if error_surface_state(page) != "clear":
        return None
    if find_input(page) is None or not turn_terminal(page, node):
        return None
    text = node.inner_text()  # errors must invalidate, never become empty success
    text = _ASSISTANT_LABEL_RE.sub("", text, count=1)  # 스크린리더용 "ChatGPT 답변:" 접두어는 응답 본문이 아니다
    if not text.strip():
        return None
    return (tuple(sorted(node_ids(node))), text)


def validate_manifest_file(path):
    if path.name == ".env" or path.name.startswith(".env.") or path.resolve().name.startswith(".env"):
        raise ValueError("환경 파일 거부")
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("manifest object required")
    parsed = urllib.parse.urlsplit(loaded.get("chat_url") or "")
    if (parsed.scheme != "https" or parsed.hostname != "chatgpt.com" or parsed.username or parsed.port
            or not CONV_URL_RE.search(parsed.path)):
        raise ValueError("manifest URL")
    if loaded.get("schema_version") == 2:
        if not isinstance(loaded.get("original_run_bound"), bool):
            raise ValueError("binding type")
        for key in ("sent_user_ids", "assistant_ids"):
            if not isinstance(loaded.get(key), list) or not all(isinstance(i, str) and i for i in loaded[key]):
                raise ValueError("binding ids")
        if not loaded["sent_user_ids"]:
            raise ValueError("전송 user 미결속 — 수동 URL 회수 필요")
        if loaded["original_run_bound"]:
            for key in ("baseline_user_ids", "baseline_assistant_ids"):
                if not isinstance(loaded.get(key), list) or not all(isinstance(i, str) for i in loaded[key]):
                    raise ValueError("전송 기준 identity 없음")
    elif "schema_version" in loaded or not isinstance(loaded.get("run_tag"), str):
        raise ValueError("unknown manifest schema")
    return loaded


def recovery_hint(binding, manifest_path) -> str:
    try:
        disk = validate_manifest_file(manifest_path)
        same_run = all(disk.get(k) == binding.get(k) for k in ("run_id", "chat_url", "original_run_bound"))
        same_user = not binding.get("sent_user_ids") or disk["sent_user_ids"] == binding["sent_user_ids"]
        if not binding.get("checkpoint_error") and disk.get("schema_version") == 2 and same_run and same_user:
            return f"\n   저장된 결속으로 회수: pack_and_ask.py --harvest '{manifest_path}'"
    except Exception:
        pass
    if binding.get("chat_url"):
        return ("\n   유효한 저장 결속 미확인 — 원 실행 복구가 아닌 수동 latest-user 회수 대안:"
                f"\n   pack_and_ask.py --harvest '{binding['chat_url']}'")
    return "\n   대화 위치 미확인 — 프로젝트에서 전송 여부를 먼저 확인하세요."


def bound_user_is_latest(page, binding: dict) -> bool:
    """강제답변 버튼은 페이지의 첫 '답변 받기' 행을 누르므로 대상 턴을 지정하지 못한다(독립 리뷰 F6). 결속된 user가
    대화의 마지막 user일 때만(= 생성 중인 턴이 결속된 턴일 때만) 허용하고, 확인하지 못하면 누르지 않는다."""
    try:
        users = message_nodes(page, "user")
        return bool(users) and node_ids(users[-1]) == set(binding.get("sent_user_ids", []))
    except Exception:
        return False


def wait_for_turn_response(page, force_after=None, max_wait=None,
                           base_user=0, base_assistant=0, base_copy=0, conv_url=None,
                           base_ids=None, skip_sent_check=False, on_bound=None,
                           binding=None, persist=None, save_response=None):
    if binding is None:
        raise RuntimeError("회수 binding 필수")
    start = time.monotonic()
    deadline = start + (max_wait or MAX_WAIT_SECS)
    url_deadline = min(deadline, start + CONV_URL_CAPTURE_SECS)
    error_since = None
    stable_since = None
    last = None
    previous_binding = None
    last_status = -STATUS_INTERVAL
    forced = False
    while time.monotonic() < deadline:
        now = time.monotonic()
        elapsed = now - start
        if detect_quota_block(page):
            binding["last_wait_status"] = "quota"
            if persist:
                persist(binding)
            return "quota", "", binding.get("chat_url")
        surface = error_surface_state(page)
        if surface == "error":
            if error_since is None:
                error_since = now
            if now - error_since >= VISIBLE_ERROR_GRACE_SECS:
                binding["last_wait_status"] = "visible_error"
                if persist:
                    persist(binding)
                print("    가시적 오류/로그인 표면 지속 — 회수 중단, 같은 대화 확인 필요", flush=True)
                return "error", "", binding.get("chat_url")
        else:
            error_since = None  # unknown is not proof of a persistent error
        if surface != "clear":
            stable_since, last = None, None
        if not binding.get("chat_url"):
            url = current_url(page)
            if CONV_URL_RE.search(url):
                binding["chat_url"] = url
                if on_bound:
                    on_bound(url)
            else:
                if elapsed - last_status >= STATUS_INTERVAL:
                    print(f"    {int(elapsed)}s | phase=WAIT_CONVERSATION_URL | error_surface={surface}"
                          f" | limit={int(url_deadline - start)}s", flush=True)
                    last_status = elapsed
                if now >= url_deadline:
                    binding["last_wait_status"] = "sent_unknown_location"
                    if persist:
                        persist(binding)
                    return "sent_unknown_location", "", None
                time.sleep(0.5)
                continue
        try:
            snapshot = response_snapshot(page, binding, persist=persist) if surface == "clear" else None
        except RuntimeError:
            raise  # identity changes must not silently choose another response
        except Exception:
            snapshot = None
        serialized = json.dumps(binding, sort_keys=True)
        if serialized != previous_binding:
            if persist:
                persist(binding)
            previous_binding = serialized
        now = time.monotonic()
        elapsed = now - start
        observed_stream = streaming_state(page)
        if observed_stream != "absent":
            snapshot = None
            stable_since, last = None, None
        if (force_after and elapsed >= force_after and not forced and binding.get("sent_user_ids")
                and surface == "clear" and observed_stream == "streaming"
                and bound_user_is_latest(page, binding)):
            forced = click_answer_now(page)
            if forced:
                binding["forced_answer"] = True
                if persist:
                    persist(binding)
                stable_since, last = None, None
        interval = STATUS_INTERVAL if elapsed < 60 else 60
        if elapsed - last_status >= interval:
            print(f"    {int(elapsed)}s | phase={binding.get('phase')} | streaming={observed_stream}"
                  f" | terminal={'y' if snapshot else 'n'} | error_surface={surface}", flush=True)
            last_status = elapsed
        if snapshot is None or elapsed < MIN_WAIT_SECS:
            stable_since, last = None, None
        elif snapshot != last or stable_since is None:
            stable_since, last = now, snapshot
        elif now - stable_since >= STABLE_CHECK_SECS:
            if response_snapshot(page, binding, persist=persist) != snapshot:
                stable_since, last = None, None
                continue
            if save_response and not save_response(page, binding, snapshot):
                stable_since, last = None, None
                continue
            return "ok", snapshot[1], binding["chat_url"]
        time.sleep(0.5)
    status = "timeout" if binding.get("chat_url") else "sent_unknown_location"
    binding["last_wait_status"] = status
    if persist:
        persist(binding)
    return status, "", binding.get("chat_url")


# ===========================================================================
# 4) 로그인된 context 선택 (fail-closed)
# ===========================================================================
def pick_context(browser):
    """인증 세션 쿠키(__Secure-next-auth*)가 있는 context를 1순위로. 그다음 chatgpt.com 쿠키 보유,
    끝으로 contexts[0]. context 자체가 없으면 None. (최종 로그인 판정은 looks_logged_in이 fail-closed로 한 번 더.)"""
    if not browser.contexts:
        return None
    # 1순위: 진짜 인증 쿠키(아무 쿠키나 X — 익명 분석쿠키로 오인 방지)
    for ctx in browser.contexts:
        try:
            cookies = ctx.cookies("https://chatgpt.com")
            if any(str(c.get("name", "")).startswith("__Secure-next-auth") for c in cookies):
                return ctx
        except Exception:
            continue
    # 2순위: chatgpt.com 쿠키가 하나라도 있는 context
    for ctx in browser.contexts:
        try:
            if ctx.cookies("https://chatgpt.com"):
                return ctx
        except Exception:
            continue
    return browser.contexts[0]


def login_state(page, wait_secs: int = 12) -> str:
    """로그인 3단계 판정: 'ok' | 'no' | 'unknown'.
    - 'no': 로그인 벽(로그인 버튼 등)이 실제로 보일 때만 — 이것만이 재로그인 요구의 근거.
    - 'ok': 입력창 + 인증 세션에서만 렌더되는 컴포저 어포던스(모델 pill/파일 input) 확인.
    - 'unknown': wait_secs 동안 둘 다 안 보임(SPA 로딩 지연/CF 챌린지/UI 변경).
      기존엔 이 경우를 'no'로 오판해 멀쩡한 세션에 재로그인을 반복 요구했다(거짓 음성).
    판정 전체를 폴링 — 느린 환경일수록 컴포저가 늦게 떠서 단발 조회는 오판한다."""
    deadline = time.monotonic() + wait_secs
    while True:
        for sel in LOGIN_WALL_SELECTORS:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    return "no"
            except Exception:
                continue
        try:
            if find_input(page) is not None and (
                    page.query_selector('button.__composer-pill')
                    or page.query_selector(FILE_INPUT_SELECTOR)):
                return "ok"
        except Exception:
            pass
        if time.monotonic() >= deadline:
            return "unknown"
        time.sleep(0.5)


def looks_logged_in(page) -> bool:
    """전송 경로용 fail-closed 래퍼 — 'ok'만 통과(unknown도 전송 안 함)."""
    return login_state(page) == "ok"


# ===========================================================================
# 3.9) ChatGPT 프로젝트 그룹핑 — 폴더명 프로젝트로 채팅 정리 (캐시→탐색→생성)
# 일반 채팅 목록이 매 실행마다 쌓이는 걸 막고, 폴더별로 채팅을 프로젝트 안에 묶는다.
# 프로젝트 홈 화면에도 컴포저(#prompt-textarea)·파일첨부(input[type=file])·모델 pill이
# 그대로 있어, 프로젝트 URL로 goto만 하면 이후 첨부/모델검증/전송/회수 로직은 변경 없이 동작.
# ===========================================================================
def _load_project_cache(cache_path: Path) -> dict:
    try:
        return json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_project_cache(cache_path: Path, cache: dict) -> None:
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        # 프로세스별 고유 tmp — 고정 이름(.json.tmp)은 동시 실행 시 서로의 tmp를 replace/삭제한다
        tmp = cache_path.with_name(f".{cache_path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, cache_path)  # 원자적 저장
    except Exception:
        pass


@contextmanager
def project_cache_lock(cache_path: Path, timeout: int = 90):
    """projects.json의 read-check-find-create-write 임계구역 직렬화(디렉터리 lock, 표준 라이브러리만).
    lock 없이는 두 프로세스가 같은 dict를 읽고 각자 저장해 삭제가 되살아나거나 키가 유실되고,
    둘 다 '없음' 판정 후 같은 이름의 원격 프로젝트를 중복 생성한다. 10분 넘은 lock은 죽은 프로세스로 보고 회수."""
    lock_dir = cache_path.with_name(cache_path.name + ".lock")
    deadline = time.monotonic() + timeout
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            lock_dir.mkdir()
            break
        except FileExistsError:
            try:
                if time.time() - lock_dir.stat().st_mtime > 600:
                    shutil.rmtree(lock_dir, ignore_errors=True)
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError(f"project cache lock timeout: {lock_dir}")
            time.sleep(0.2)
    try:
        yield
    finally:
        shutil.rmtree(lock_dir, ignore_errors=True)


# 워크스페이스 이동(개인→팀 등)으로 접근 불가가 된 프로젝트는 URL이 유지된 채
# 에러 모달만 뜨는 케이스가 있어, URL 존재만으로는 생존 판정이 안 된다.
_PROJECT_ACCESS_ERROR_RE = (
    r"이 프로젝트에 액세스할 수 없습니다|can.t access this project|"
    r"don.t have access|올바른 계정으로 로그인|プロジェクトにアクセスできません"
)
_PROJECT_ID_RE = re.compile(r"(g-p-[0-9a-f]{32})", re.I)
PROJECT_OK, PROJECT_DEAD, PROJECT_AUTH, PROJECT_UNKNOWN = "ok", "dead", "auth", "unknown"


def find_visible_input(page):
    """가시적·활성 컴포저만(#prompt-textarea 우선). 에러 모달 아래 숨은 컴포저·다른 contenteditable을 통과시키지 않는다."""
    for sel in INPUT_SELECTORS:
        try:
            for el in page.query_selector_all(sel):
                if el.is_visible() and el.is_enabled():
                    return el
        except Exception:
            continue
    return None


def visible_alert_dialog_text(page) -> str:
    """가시적 에러 표면([role=dialog|alert]) 텍스트만 — 본문 전체를 보면 채팅 제목/프롬프트 속 문구에 오탐한다."""
    parts = []
    for sel in ('[role="dialog"]', '[role="alert"]'):
        try:
            for node in page.query_selector_all(sel):
                if node.is_visible():
                    parts.append(node.inner_text() or "")
        except Exception:
            continue
    return "\n".join(parts)


_PROJECT_PATH_RE = re.compile(r"^/g/g-p-[0-9a-f]{32}(?:-[^/?#]*)?(?:/project)?/?$", re.I)


def chatgpt_origin_ok(url: str | None) -> bool:
    """https://chatgpt.com(자격정보·포트 없음)만 인정. URL 문자열에 프로젝트 id가 들어 있다는 사실만으로는 부족하다."""
    try:
        parsed = urllib.parse.urlsplit(url or "")
        return (parsed.scheme == "https" and parsed.hostname == "chatgpt.com"
                and not parsed.username and not parsed.password and parsed.port is None)
    except ValueError:
        return False


def project_url_ok(url: str | None) -> bool:
    """프로젝트 홈 URL 형태: chatgpt.com의 /g/g-p-<32hex>[-slug][/project]. 캐시·탐색 결과 모두 이동 전에 검증한다."""
    if not chatgpt_origin_ok(url):
        return False
    return bool(_PROJECT_PATH_RE.match(urllib.parse.urlsplit(url).path))


def project_home_state(page, url: str, probe_secs: int = 15) -> str:
    """프로젝트 URL 생존을 4상태로 판정(2초 단발 → 폴링 + 연속 안정 구간).
    ok: 그 g-p id가 URL에 유지 + 가시 컴포저 + 차단 다이얼로그 없음이 4초 연속.
    dead: id 불일치(홈 리다이렉트)·명시적 403/404·접근불가 문구 — 현 워크스페이스에서 재탐색/재생성 대상.
    auth: 로그인 벽. unknown: 지연·네트워크·UI 변경 — 캐시 삭제·프로젝트 생성 모두 금지(fail-closed).
    False 하나로 뭉개면 일시 오류에 정상 캐시를 지우고 중복 프로젝트를 만든다(2026-08-24 GPT Pro 리뷰)."""
    if not project_url_ok(url):
        return PROJECT_DEAD  # 외부 origin/비정상 경로의 캐시·후보는 이동하지 않고 죽은 값으로 취급(재탐색 대상)
    m = _PROJECT_ID_RE.search(url)
    if not m:
        return PROJECT_UNKNOWN  # 파싱 실패는 identity 검사 생략 사유가 아니다
    gp_id = m.group(1).lower()
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except Exception:
        return PROJECT_UNKNOWN
    try:
        if resp is not None:
            if resp.status == 401:
                return PROJECT_AUTH
            if resp.status in (403, 404):
                return PROJECT_DEAD
    except Exception:
        pass
    deadline = time.monotonic() + probe_secs
    healthy_since = None
    final_url = ""
    while time.monotonic() < deadline:
        try:
            final_url = current_url(page)
            if _q(page, LOGIN_WALL_SELECTORS) is not None:
                return PROJECT_AUTH
            blocking = visible_alert_dialog_text(page)
            if re.search(_PROJECT_ACCESS_ERROR_RE, blocking, re.I):
                return PROJECT_DEAD
            if (chatgpt_origin_ok(final_url) and gp_id in final_url.lower() and not blocking
                    and find_visible_input(page) is not None):
                if healthy_since is None:
                    healthy_since = time.monotonic()
                elif time.monotonic() - healthy_since >= 4:
                    return PROJECT_OK
            else:
                healthy_since = None
        except Exception:
            healthy_since = None
        time.sleep(0.5)
    if final_url and (not chatgpt_origin_ok(final_url) or gp_id not in final_url.lower()):
        return PROJECT_DEAD
    return PROJECT_UNKNOWN


# 다국어(사용자 ChatGPT UI 언어) 베스트에포트 — '새 프로젝트' 버튼 / '만들기' 제출 버튼.
_NEW_PROJECT_RE = r"새 프로젝트|New project|新規プロジェクト|プロジェクトを追加|Add project|Create project"
_CREATE_SUBMIT_RE = r"프로젝트 만들기|Create project|プロジェクトを作成|^Create$|^作成$|^만들기$"


def find_project_url_api(page, name: str) -> str | None:
    """현 워크스페이스의 프로젝트를 백엔드 API로 표시이름 정확 일치 조회 → 홈 URL 구성.
    실측 2026-08-25: 프로젝트가 사이드바에 a[href] 링크로 렌더되지 않고 '프로젝트' 페이지 뒤로 이동
    → DOM 탐색이 구조적으로 실패. API가 언어·가상화·접힘 무관하고 결정적이라 1순위.
    chatgpt.com 오리진 페이지에서만 동작. 실패는 None(호출자가 DOM 폴백)."""
    try:
        short = page.evaluate("""async (nm) => {
            try {
                const sess = await (await fetch('/api/auth/session', {credentials: 'include'})).json();
                if (!sess || !sess.accessToken) return null;
                const r = await fetch('/backend-api/gizmos/snorlax/sidebar?conversations_per_gizmo=0',
                                      {credentials: 'include', headers: {Authorization: 'Bearer ' + sess.accessToken}});
                if (!r.ok) return null;
                const j = await r.json();
                for (const it of (j.items || [])) {
                    const g = it.gizmo && it.gizmo.gizmo;
                    if (g && g.display && g.display.name === nm && g.short_url) return g.short_url;
                }
            } catch (e) {}
            return null;
        }""", name)
        return f"{CHATGPT_URL}g/{short}/project" if short else None
    except Exception:
        return None


def find_project_url(page, name: str) -> str | None:
    """사이드바에서 '표시 이름이 정확히 name'인 프로젝트의 홈 URL을 회수(SPA 라우팅). 없으면 None.
    언어무관: 행(li)의 표시텍스트 == 이름으로 찾고(aria 로컬라이즈에 의존 안 함),
    같은 행의 '이름이 안 들어간 버튼'(=홈 버튼; 옵션버튼 aria엔 이름이 들어감)을 클릭한다.
    #2 대응: 목표가 보일 때까지 사이드바를 스크롤하며 폴링 → 가상화/지연으로 못 찾고 중복 생성하는 일 방지.
    #3 대응: 어떤 예외도 삼켜 None 반환(폴백 가능)."""
    try:
        for _ in range(12):
            # 1순위: 사이드바 안의 실제 href(클릭 휴리스틱보다 결정적) — 이름 정확 일치
            href = page.evaluate("""(nm) => {
                for (const root of document.querySelectorAll('nav, aside')) {
                    for (const a of root.querySelectorAll('a[href*="/g/g-p-"]')) {
                        const t = (a.innerText || a.textContent || '').replace(/\\s+/g, ' ').trim();
                        if (t === nm) return new URL(a.getAttribute('href'), location.origin).href;
                    }
                }
                return null;
            }""", name)
            if href:
                return href
            # 2순위(button-only UI): 사이드바 행의 표시텍스트 == 이름 → 홈 버튼 클릭(문서 전체 li는 보지 않음)
            clicked = page.evaluate("""(nm) => {
                const lis = [...document.querySelectorAll('nav li, aside li')];
                for (const li of lis) {
                    const first = ((li.innerText || '').trim().split('\\n')[0] || '').trim();
                    const btns = [...li.querySelectorAll('button[aria-label]')];
                    if (first === nm && btns.length) {
                        // 옵션버튼 aria엔 프로젝트명이 들어감 → 이름이 '안' 들어간 버튼이 홈(내비) 버튼
                        const home = btns.find(b => !((b.getAttribute('aria-label') || '').includes(nm))) || btns[0];
                        home.click();
                        return true;
                    }
                }
                return false;
            }""", name)
            if clicked:
                try:
                    page.wait_for_url("**/g/g-p-**", wait_until="commit", timeout=8000)
                except Exception:
                    pass
                time.sleep(1.2)
                u = current_url(page)  # page.url은 SPA pushState를 반영 못 함(스테일)
                return u if "/g/g-p-" in u else None
            # 가상화/접힘 대비: 한 화면씩 내려가며 재탐색(끝으로 점프하면 목록 중간을 건너뛴다)
            moved = page.evaluate("""() => { let moved = false;
                for (const el of document.querySelectorAll('nav *, aside *')) {
                    if (el.scrollHeight > el.clientHeight + 20) {
                        const next = Math.min(el.scrollHeight - el.clientHeight, el.scrollTop + Math.max(200, el.clientHeight * 0.8));
                        if (next > el.scrollTop) { el.scrollTop = next; moved = true; }
                    }
                }
                return moved; }""")
            if not moved:
                return None
            time.sleep(0.5)
    except Exception:
        return None
    return None


def create_project(page, name: str) -> str | None:
    """'새 프로젝트' 모달로 폴더명 프로젝트 생성 → 홈 URL 반환. 실패/미지원 시 None(호출자 폴백).
    제출은 다국어 텍스트 매칭 → 실패하면 Enter 폴백(언어무관)."""
    opened = page.evaluate("""(re) => { const rx = new RegExp(re, 'i');
        const b = [...document.querySelectorAll('button[aria-label]')].find(x => rx.test(x.getAttribute('aria-label') || ''));
        if (b) { b.click(); return true; } return false; }""", _NEW_PROJECT_RE)
    if not opened:
        return None  # '새 프로젝트' 버튼 없음(프로젝트 미지원 플랜/언어 불일치) → 일반 채팅 폴백
    try:
        # 모달의 유일한 visible text-input = 이름칸(컴포저는 contenteditable이라 input[type=text] 아님)
        name_input = page.locator('input[type="text"]:visible').last
        name_input.wait_for(state="visible", timeout=8000)
        name_input.click()
        name_input.fill(name)        # fill로 입력해야 제출 버튼이 enabled 된다
        time.sleep(0.4)
        submitted = page.evaluate("""(re) => { const rx = new RegExp(re, 'i');
            const btns = [...document.querySelectorAll('button')].filter(b => !b.disabled && rx.test((b.innerText || '').trim()));
            if (btns.length) { btns[btns.length - 1].click(); return true; } return false; }""", _CREATE_SUBMIT_RE)
        if not submitted:
            name_input.press("Enter")  # 텍스트 매칭 실패 시 언어무관 폴백
        page.wait_for_url("**/g/g-p-**", wait_until="commit", timeout=15000)
        time.sleep(2)
        u = current_url(page)
        return u if "/g/g-p-" in u else None
    except Exception:
        try:
            page.keyboard.press("Escape")  # 모달 닫고 폴백
        except Exception:
            pass
        return None


def ensure_project(page, name: str, cache_key: str, cache_path: Path) -> str | None:
    """프로젝트 홈 URL 확보: 캐시(절대경로 키) → 사이드바 탐색 → 생성.
    #1 대응: 캐시 키는 '절대경로'(cache_key) — 같은 폴더명의 다른 경로가 캐시를 공유하지 않는다.
    #3 대응: 함수 전체를 try/except로 감싸 어떤 예외도 None으로(호출자가 일반 채팅으로 폴백)."""
    try:
        with project_cache_lock(cache_path):
            return _ensure_project_locked(page, name, cache_key, cache_path)
    except Exception:
        return None


def _open_chat_home(page) -> bool:
    page.goto(CHATGPT_URL, wait_until="domcontentloaded", timeout=30000)
    for _ in range(10):
        if find_visible_input(page) is not None:
            return True
        time.sleep(1)
    return False


def current_workspace_id(page) -> str | None:
    """활성 ChatGPT 워크스페이스 id — `_account` 쿠키(실측 2026-08-25, localStorage `_account`와 동일).
    개인↔팀 등 워크스페이스 전환이 프로젝트 접근성을 통째로 바꾸므로(2026-08-11 실사고) 캐시에 결속한다.
    실패는 None — 판정 강화용 신호일 뿐, None이면 기존 URL 검증 경로만으로 동작한다."""
    try:
        val = page.evaluate(
            """() => {
                const c = document.cookie.split('; ').find(x => x.startsWith('_account='));
                if (c) return decodeURIComponent(c.split('=').slice(1).join('='));
                try { return JSON.parse(localStorage.getItem('_account') || 'null'); } catch (e) { return null; }
            }""")
        return val or None
    except Exception:
        return None


def _cache_record(value):
    """캐시 값 하위호환 파서: 구형 문자열(url) / 신형 dict({url, workspace_id}) → (url, workspace_id)."""
    if isinstance(value, dict):
        return value.get("url"), value.get("workspace_id")
    return value, None


def _ensure_project_locked(page, name: str, cache_key: str, cache_path: Path) -> str | None:
    """정책: ok→사용 / auth→중단 / unknown→캐시 보존·생성 금지(이번 런은 일반채팅 폴백) /
    dead→현 워크스페이스에서 탐색→생성, 대체물이 검증된 뒤에만 옛 캐시 제거.
    탐색·생성으로 얻은 URL도 같은 validator를 통과해야 캐시에 들어간다(오클릭·늦은 리다이렉트 고착 방지).
    워크스페이스 결속(P2): 캐시에 workspace_id를 저장, 현재 워크스페이스와 다르면 goto 없이 즉시 dead
    (죽은 URL을 열어 에러 팝업을 띄우는 단계 자체를 생략)."""
    ws_now = current_workspace_id(page)
    cache = _load_project_cache(cache_path)
    cached_rec = cache.get(cache_key)
    cached_url, cached_ws = _cache_record(cached_rec)
    cached_state = PROJECT_UNKNOWN
    if cached_url:
        if cached_ws and ws_now and cached_ws != ws_now:
            cached_state = PROJECT_DEAD
            print(f"  ℹ️  워크스페이스 변경 감지(캐시={cached_ws[:8]}… ≠ 현재={ws_now[:8]}…) → 현 워크스페이스에서 재탐색/재생성")
        else:
            cached_state = project_home_state(page, cached_url)
            if cached_state == PROJECT_OK:
                if ws_now and cached_ws != ws_now:
                    cache[cache_key] = {"url": cached_url, "workspace_id": ws_now}  # 구형 레코드 승격
                    _save_project_cache(cache_path, cache)
                return cached_url
            if cached_state == PROJECT_AUTH:
                return None
            print(f"  ℹ️  캐시된 프로젝트 판정={cached_state}" + (" → 재탐색/재생성" if cached_state == PROJECT_DEAD else " → 캐시 보존, 이번 런은 폴백"))
            if cached_state == PROJECT_UNKNOWN:
                return None

    # 탐색으로 찾은 후보도 4상태를 유지한다(독립 리뷰 F4/N5): 존재가 확인된 프로젝트의 unknown/auth를 '없음'으로
    # 바꾸면 같은 이름의 새 프로젝트를 만들고 캐시를 갈아끼워 이후 리뷰가 기존 프로젝트와 분리된다.
    # 명시적으로 dead(없음 확정)이거나 후보 자체가 없을 때만 다음 단계/생성으로 진행한다.
    def vet(found):
        if not found:
            return None, None
        state = project_home_state(page, found)
        return (found if state == PROJECT_OK else None), state

    candidate, state = vet(find_project_url_api(page, name))  # API 1순위(현 오리진 페이지에서 즉시)
    if state in (PROJECT_UNKNOWN, PROJECT_AUTH):
        print(f"  ℹ️  탐색한 프로젝트 판정={state} → 새 프로젝트를 만들지 않고 이번 런은 폴백")
        return None
    if not candidate:
        if not _open_chat_home(page):
            return None
        candidate, state = vet(find_project_url(page, name))  # DOM 폴백(구 UI/API 실패 대비)
        if state in (PROJECT_UNKNOWN, PROJECT_AUTH):
            print(f"  ℹ️  탐색한 프로젝트 판정={state} → 새 프로젝트를 만들지 않고 이번 런은 폴백")
            return None
    if not candidate:
        if not _open_chat_home(page):
            return None
        created = create_project(page, name)
        candidate = created if created and project_home_state(page, created) == PROJECT_OK else None

    latest = _load_project_cache(cache_path)  # lock 안이지만 재읽기 — 항상 최신 dict에 갱신
    if candidate:
        latest[cache_key] = {"url": candidate, "workspace_id": ws_now} if ws_now else candidate
        _save_project_cache(cache_path, latest)
        return candidate
    if cached_url and cached_state == PROJECT_DEAD and latest.get(cache_key) == cached_rec:
        latest.pop(cache_key, None)  # 대체물을 못 얻었어도 확정 사망 캐시는 제거(에러 팝업 반복 방지)
        _save_project_cache(cache_path, latest)
    return None


# ===========================================================================
# main
# ===========================================================================
def _main():
    ap = argparse.ArgumentParser(description="repomix → 구독 ChatGPT(GPT Pro, 최신 플래그십) 분석")
    ap.add_argument("--target", default=None, help="분석 대상 폴더(생략 시 프롬프트만 = 의견 모드)")
    ap.add_argument("--include", default=None, help='repomix --include 글롭')
    ap.add_argument("--ignore", default=None, help="repomix --ignore 글롭")
    ap.add_argument("--compress", action="store_true",
                    help="tree-sitter 골격만(토큰 절감) — 본문 제거되니 정확성 리뷰엔 쓰지 마라")
    ap.add_argument("--no-line-numbers", action="store_true",
                    help="라인번호 prefix 끄기(기본 on — AI가 파일:라인 인용하도록)")
    ap.add_argument("--style", default="markdown", choices=["xml", "markdown", "plain"])
    ap.add_argument("--token-budget", type=int, default=None)
    ap.add_argument("--attach", action="store_true",
                    help="첨부 강제 — 첨부 실패 시 붙여넣기 폴백 없이 중단(기본은 작은 pack에 한해 인라인 폴백)")
    ap.add_argument("--prompt", default=None)
    ap.add_argument("--prompt-file", default=None)
    ap.add_argument("--model", default=None, help='추론단계 선택(예: "pro")')
    ap.add_argument("--require-model", default=None,
                    help='모델명 검증(예: "GPT-5.6") — 불일치 시 전송 중단')
    ap.add_argument("--force-answer-after", type=int, default=None,
                    help="N초 후 리즈닝 중이면 '지금 답변 받기' 재시도")
    ap.add_argument("--max-wait", type=int, default=None,
                    help=f"응답 최대 대기 초(기본 {MAX_WAIT_SECS}=20분; env INSANE_REVIEW_MAX_WAIT로도 설정)")
    ap.add_argument("--browser", default=None,
                    help="자동화에 쓸 브라우저(이름: chrome/comet/brave/edge/chromium/vivaldi 또는 절대경로). "
                         "생략 시 config 저장값 → 첫 감지 브라우저. 항상 전용 프로필로 실행")
    ap.add_argument("--list-browsers", action="store_true",
                    help="이 OS에 설치된 크로미움 계열 브라우저 목록 출력(BROWSERS 라인)")
    ap.add_argument("--launch-browser", default=None, metavar="NAME|PATH",
                    help="지정 브라우저를 전용 프로필+디버그포트로 실행(빈 문자열이면 자동 선택). 성공 시 config에 저장")
    ap.add_argument("--set-launch-mode", default=None, choices=list(LAUNCH_MODES),
                    help="전용 브라우저 실행 방식을 config에 저장(최초 1회 선택). "
                         "foreground=창 뜨고 포커스 가져감 / background=창 뜨되 포커스 안 뺏음(mac) / headless=창 없음")
    ap.add_argument("--project", default=None,
                    help="채팅을 묶을 ChatGPT 프로젝트 이름(기본: 현재 폴더명). 폴더별로 채팅이 프로젝트 안에 정리됨")
    ap.add_argument("--no-project", action="store_true",
                    help="프로젝트 그룹핑 비활성화 — 일반 새 채팅으로 전송(기존 동작)")
    ap.add_argument("--pack-only", action="store_true")
    ap.add_argument("--keep-pack", action="store_true", help="전송 후 패킹 파일 보존(기본은 유지; 끄려면 --delete-pack)")
    ap.add_argument("--delete-pack", action="store_true", help="응답 회수 후 패킹 파일 삭제(시크릿 위생)")
    ap.add_argument("--out-dir", default=None,
                    help="출력 저장 폴더(기본: 현재 프로젝트의 .insane-review/; env INSANE_REVIEW_OUT)")
    ap.add_argument("--check-env", action="store_true")
    ap.add_argument("--ensure-env", action="store_true",
                    help="저장된 브라우저가 있고 CDP가 닫혀(down) 있으면 조용히 1회 자동 기동 후 점검 "
                         "(저장값-only·첫감지 폴백 없음; browser=wrong이면 자동기동 안 함)")
    ap.add_argument("--install", action="store_true")
    ap.add_argument("--council", action="store_true",
                    help="agent-council 멤버 모드: 로그는 stderr, 응답만 stdout")
    ap.add_argument("--harvest", default=None, metavar="CHAT_URL|MANIFEST",
                    help="전송 없이 기존 대화에서 완료된 응답만 회수(타임아웃 시 안내된 대화 URL 또는 manifest_*.json 경로)")
    ap.add_argument("--retries", type=int, default=1)
    ap.add_argument("prompt_args", nargs="*", help="프롬프트(위치인자 — council 호환)")
    args = ap.parse_args()

    if args.check_env:
        sys.exit(check_env(do_install=args.install))

    if args.ensure_env:
        # 저장값-only 자동기동: CDP가 '닫힘'(down)이고 저장된 브라우저가 해석되면 한 번만 띄운다.
        # browser=wrong(포트를 다른 프로세스가 점유)이거나 저장값이 없으면 자동기동하지 않고,
        # check_env가 상태만 보고한다 → 커맨드가 그때만 사용자에게 묻는다(최초 1회 온보딩).
        if not is_port_open(CDP_PORT):
            saved = _load_config().get("browser")
            if saved:
                r = resolve_browser(saved)   # 인자 지정 경로 → 첫감지 폴백 없음(저장값-only)
                if r:
                    launch_browser_exe(r[1], r[0])
        sys.exit(check_env(do_install=args.install))

    if args.list_browsers:
        bs = detect_browsers()
        print("BROWSERS " + ",".join(f"{n}={p}" for n, p in bs))
        for n, p in bs:
            print(f"  • {n}: {p}")
        if not bs:
            print("  (설치된 크로미움 계열 브라우저를 찾지 못함)")
        sys.exit(0)

    if args.set_launch_mode:
        save_launch_mode(args.set_launch_mode)
        print(f"LAUNCH_MODE {args.set_launch_mode} 저장됨 (~/.insane-review/config.json)")
        if args.set_launch_mode == "headless":
            print("  주의: ChatGPT가 헤드리스를 차단하면 로그인·전송이 실패할 수 있다. "
                  "--check-env로 login=ok 확인 후 사용할 것.")
        return 0

    if args.launch_browser is not None:
        resolved = resolve_browser(args.launch_browser or None)
        if not resolved:
            avail = ", ".join(n for n, _ in detect_browsers()) or "없음"
            sys.exit(f"❌ 브라우저를 찾지 못함 (지정='{args.launch_browser}', 감지=[{avail}])")
        name, path = resolved
        if launch_browser_exe(path, name):
            save_browser_choice(name)
            print(f"STATUS_LAUNCH ok browser={name}")
            sys.exit(0)
        sys.exit("❌ 브라우저 실행/CDP 확인 실패")

    # --require-model은 모델 검증 경로(select_model)에서만 효력 → --model 없이 단독 사용 시 검증이 통째로
    # 스킵되는 fail-open을 차단(fail-closed). 모델/추론단계를 함께 지정해야 검증이 돈다.
    if args.require_model and not args.model:
        sys.exit('❌ --require-model은 --model과 함께 써야 합니다(모델/추론단계를 선택·검증하는 경로).\n'
                 '     예: --model pro --require-model "GPT-5.6"')

    # --harvest: 전송 없이 기존 대화에서 회수만 — 패킹/프롬프트/프로젝트 진입 불필요
    harvest_url = None
    recovery_binding = None
    if args.harvest:
        _h = Path(args.harvest).expanduser()
        if _h.name == ".env" or _h.name.startswith(".env."):
            sys.exit("❌ 환경 파일은 manifest로 읽지 않습니다")
        if _h.exists():
            try:
                loaded = validate_manifest_file(_h)
                harvest_url = loaded.get("chat_url")
                if loaded.get("schema_version") == 2:
                    recovery_binding = loaded
                else:
                    print("  legacy manifest → 수동 latest-user 회수; 원 실행 identity 미확인", flush=True)
                    recovery_binding = {"source_manifest": str(_h), "original_run_bound": False}
            except Exception:
                sys.exit(f"❌ manifest 파싱 실패: {_h}")
        else:
            harvest_url = args.harvest
        parsed = urllib.parse.urlsplit(harvest_url or "")
        if (parsed.scheme != "https" or parsed.hostname != "chatgpt.com" or parsed.username or parsed.port
                or not CONV_URL_RE.search(parsed.path)):
            sys.exit(f"❌ --harvest 인자가 대화 URL(/c/<id>)이 아님: {args.harvest}")
        args.target = None  # 회수 모드는 전송이 없다 — 패킹 생략

    real_stdout = sys.stdout
    if args.council:
        sys.stdout = sys.stderr

    out_dir = Path(args.out_dir).expanduser() if args.out_dir else OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"  출력 폴더: {out_dir}")
    # 폴더명→프로젝트URL 캐시(per-repo) — 평소엔 사이드바 안 건드리고 바로 프로젝트로 goto
    project_cache_path = out_dir / "projects.json"
    # #4: 자동 이름은 '폴더명 · 경로해시8'. 원격(ChatGPT) 프로젝트 탐색은 표시이름으로만 매칭하므로,
    # 이름에 경로 식별자가 없으면 동명 다른 폴더(/a/api, /b/api)가 같은 원격 프로젝트로 병합된다.
    # 사용자가 --project로 명시하면 그 이름 그대로(사용자 의도 존중).
    if args.project:
        project_name = args.project
    else:
        _ph = hashlib.sha256(str(Path.cwd().resolve()).encode("utf-8")).hexdigest()[:8]
        project_name = f"{Path.cwd().name} · {_ph}"
    # 캐시 키 = 절대경로::이름 — 동명 다른 폴더도, 같은 폴더의 다른 --project도 충돌하지 않음
    project_cache_key = f"{Path.cwd().resolve()}::{project_name}"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_tag = f"{ts}_{os.getpid()}_{uuid.uuid4().hex[:6]}"  # 동시 실행 충돌 방지
    pack_path = None
    tokens = None
    label = "prompt"
    verified_model_name = None

    if args.target:
        target = Path(args.target).resolve()
        if not target.exists():
            sys.exit(f"❌ 대상 폴더 없음: {target}")
        label = re.sub(r"[^A-Za-z0-9_.-]", "-", target.name)
        ext = {"xml": "xml", "markdown": "md", "plain": "txt"}[args.style]
        pack_path = out_dir / f"pack_{label}_{run_tag}.{ext}"
        # 출력 폴더가 대상 안이면 이전 산출물(pack_*/response_*)이 다음 pack에 섞이는 self-inclusion 차단
        eff_ignore = args.ignore
        try:
            rel = out_dir.resolve().relative_to(target)
            rel_glob = f"{rel.as_posix()}/**"
            eff_ignore = f"{eff_ignore},{rel_glob}" if eff_ignore else rel_glob
            print(f"  ↳ 출력 폴더가 대상 내부 → ignore 자동 추가: {rel_glob}")
        except ValueError:
            pass  # 대상 밖 → self-inclusion 없음
        print(f"\n[1/3] repomix 패킹 — {label}")
        pack_path, tokens = pack_repo(
            target, include=args.include, ignore=eff_ignore, compress=args.compress,
            style=args.style, token_budget=args.token_budget, out_path=pack_path,
            line_numbers=not args.no_line_numbers)
        if args.pack_only:
            print(f"\n[pack-only] 산출물: {pack_path}")
            return
    else:
        if args.pack_only:
            sys.exit("❌ --pack-only는 --target이 필요합니다.")
        print("\n[프롬프트-only] 레포 없이 질문만 전송")

    if sync_playwright is None:
        sys.exit("❌ playwright 미설치. pip install playwright")
    if pyperclip is None:
        print("⚠️  pyperclip 미설치 — 붙여넣기/복사회수 신뢰도 하락")

    positional = " ".join(args.prompt_args).strip() if args.prompt_args else ""
    prompt = (args.prompt or positional
              or (Path(args.prompt_file).read_text(encoding="utf-8") if args.prompt_file else None)
              or DEFAULT_PROMPT)
    if harvest_url:
        label = "harvest"
        prompt = f"(harvest) {harvest_url}"

    resolved_browser = resolve_browser(args.browser)
    bname = resolved_browser[0] if resolved_browser else (args.browser or "자동감지")
    print(f"\n[2/3] 브라우저 준비 ({bname})")
    if not ensure_browser(args.browser):
        sys.exit(1)
    # 명시적 지정(--browser)일 때만 영속화 — 자동감지 폴백을 사용자 선택처럼 굳히지 않는다.
    if args.browser and resolved_browser:
        save_browser_choice(resolved_browser[0])

    print("\n[3/3] ChatGPT 투입 & 응답 회수")
    print("  ⚠️  회수가 끝날 때까지 전용 브라우저 창을 조작하지 마세요(이탈 시 자동 복귀하지만 오염 위험)")
    response = ""
    conv_url = harvest_url          # 결속된 대화 URL — 있으면 이후 시도는 '회수 재시도'(재전송 금지)
    base_ids_snapshot: set | None = (set() if harvest_url else None)
    sent_unknown = False
    dispatch = {"state": "NOT_DISPATCHED", "reason": "preparation"}
    quota_hit = False
    surface_error = False
    manifest_path = out_dir / f"manifest_{label}_{run_tag}.json"
    resp_path = out_dir / f"response_{label}_{run_tag}.md"
    binding = recovery_binding or {}
    # 전용 Chrome 프로세스 소유권 검증(lsof/ps 기반)은 이 플러그인에 포함하지 않는다(Codex 포트 전용).
    binding["ownership_verification"] = "not_checked"
    if harvest_url:
        if not binding.get("original_run_bound"):
            print("  수동 회수: 마지막 user의 답변 선택; 원 실행 결속 미확인", flush=True)
        binding.setdefault("original_run_bound", False)
        binding.setdefault("harvest_mode", "run" if binding["original_run_bound"] else "manual_latest_user")
        binding.setdefault("binding_origin", "sent_request" if binding["original_run_bound"] else "manual_selection")
        binding.setdefault("phase", "MANUAL_SELECT")
        binding["chat_url"] = harvest_url
    else:
        binding.update(original_run_bound=True, phase="PREPARED", harvest_mode="run",
                       binding_origin="sent_request", sent_user_ids=[], assistant_ids=[])
    binding.setdefault("schema_version", 2)
    binding.setdefault("run_id", run_tag)
    binding.setdefault("sent_user_ids", [])
    binding.setdefault("assistant_ids", [])

    def save_binding(value):
        try:
            persist_binding(manifest_path, value)
        except Exception:
            binding["checkpoint_error"] = True
            raise

    def publish_response(page, value, snapshot):
        metadata = {k: value.get(k) for k in ("run_id", "harvest_mode", "original_run_bound", "chat_url",
                    "sent_user_ids", "assistant_ids", "model_verification", "ownership_verification", "forced_answer")}
        body = "# ChatGPT 응답\n\n```json\n" + json.dumps(metadata, ensure_ascii=False, indent=2) + "\n```\n\n" + snapshot[1] + "\n"
        temp = resp_path.with_name(resp_path.name + "." + uuid.uuid4().hex + ".tmp")
        with secure_create(temp) as f:
            f.write(body.encode())
            f.flush()
            os.fsync(f.fileno())
        if response_snapshot(page, value, persist=save_binding) != snapshot:
            return False
        os.replace(temp, resp_path)
        value.update(phase="COMPLETE", response_sha256=hashlib.sha256(snapshot[1].encode()).hexdigest(),
                     response_path=str(resp_path))
        save_binding(value)
        return True

    last_failure = None
    # Pro는 20~60분이 정상 범위 — 명시값(--max-wait/env) 없을 때만 기본 상향
    mw_eff = args.max_wait
    if (mw_eff is None and "INSANE_REVIEW_MAX_WAIT" not in os.environ
            and args.model and args.model.strip().lower() == "pro"):
        mw_eff = PRO_MAX_WAIT_SECS
        print(f"  ⏲  Pro 추론단계 → 최대 대기 {PRO_MAX_WAIT_SECS}s 자동 상향(--max-wait/env가 우선)")
    attempts = max(1, args.retries + 1)
    for attempt in range(1, attempts + 1):
        if response:
            break  # 회수 경로가 continue로 성공을 들고 올라온 경우
        if attempt > 1:
            print(f"  ↻ 재시도 {attempt - 1}/{args.retries} ...")
            time.sleep(3)
        try:
            with sync_playwright() as pw:
                browser = connect_cdp(pw)
                ctx = pick_context(browser)
                if ctx is None:
                    raise RuntimeError("브라우저 context 없음 (로그인된 Comet/Chrome 필요)")
                page = ctx.new_page()
                hide_browser_if_background()  # 새 탭 생성이 앱을 앞으로 끌어올리므로 즉시 재숨김
                _guard_dialogs(ctx, page)
                try:
                    if conv_url:
                        # ── 회수 경로(재전송 없음): 결속된 대화 URL로 가서 이어서/다시 대기 ──
                        # 타임아웃·예외 후 재시도와 --harvest가 모두 이 경로 — 중복 채팅 생성 원천 차단.
                        print(f"  🔁 회수 모드(재전송 없음): {conv_url}")
                        page.goto(conv_url, wait_until="load", timeout=60000)
                        time.sleep(2)
                        if login_state(page) == "no":
                            raise RuntimeError("ChatGPT 로그인 벽 감지 — 해당 브라우저에서 chatgpt.com 로그인 확인")
                        status, text, conv_url = wait_for_turn_response(
                            page, force_after=args.force_answer_after, max_wait=mw_eff,
                            conv_url=conv_url, base_ids=base_ids_snapshot, skip_sent_check=True,
                            binding=binding, persist=save_binding, save_response=publish_response)
                        if status == "quota":
                            print("  ⛔ 사용량 한도 감지 — 회수 재시도 중단(한도 해제 후 --harvest 재실행)")
                            quota_hit = True
                            break
                        if status == "error":
                            surface_error = True
                            break
                        if status == "timeout":
                            print(f"  ⚠️  타임아웃 — 다음 시도도 같은 채팅 회수 재시도: {conv_url}")
                            continue
                        if status == "ok" and text and text.strip():
                            response = text
                        else:
                            print(f"  ⚠️  응답 비었거나 너무 짧음(status={status}) → 회수 재시도")
                        continue  # 회수 경로 종결(전송 경로 진입 금지) — 성공 시 루프 상단에서 break
                    else:
                        page.goto(CHATGPT_URL, wait_until="load", timeout=60000)
                        time.sleep(3)
                        for _ in range(10):
                            if find_input(page):
                                break
                            time.sleep(1)
                        _lst = login_state(page)
                        if _lst != "ok":
                            raise RuntimeError(
                                "ChatGPT 로그인 벽 감지 — 해당 브라우저에서 chatgpt.com 로그인 확인" if _lst == "no"
                                else "ChatGPT 컴포저 미확인(로딩 지연/CF 챌린지 가능) — 전용 브라우저 창 상태 확인 후 재시도")

                        # 프로젝트 그룹핑(기본 on): 현재 폴더명 프로젝트로 채팅을 정리(일반 채팅목록 오염 방지).
                        # 어떤 실패(예외 포함)에도 하드중단 X — 컴포저가 확인되는 일반 채팅으로 폴백(#3).
                        if not args.no_project:
                            proj_url = ensure_project(page, project_name, project_cache_key, project_cache_path)
                            entered = False
                            if proj_url:
                                try:
                                    # 진입도 같은 validator — id 유지+가시 컴포저+차단 없음(숨은 컴포저로 오판 금지)
                                    entered = project_home_state(page, proj_url) == PROJECT_OK
                                except Exception as pexc:
                                    print(f"  ⚠️  프로젝트 진입 예외({str(pexc)[:50]})")
                                    entered = False
                            if entered:
                                print(f"  🗂  프로젝트 '{project_name}'에 채팅 정리 → {proj_url}")
                            else:
                                raise RuntimeError("요청 프로젝트 확인 실패 — 전송 중단")

                        # Chat/Work 게이트 — 모델 스위처를 열기 '전에' 보정한다.
                        # Work 모드엔 Pro 눈금 자체가 없어 슬라이더 인덱스 계산이 무의미해진다.
                        chat_ok, _mode_state = ensure_chat_mode(page)
                        if not chat_ok:
                            raise RuntimeError("모드 상태 미확인")

                        if args.model:
                            print("  요청된 모델/추론단계 사전검증 시작")
                            verified, v_name = select_model(page, args.model, require_model=args.require_model)
                            if not verified:
                                raise RuntimeError("모델/추론단계 사전검증 실패")
                            verified_model_name = v_name

                        # 본문은 '첨부'가 기본. 첨부 실패 시:
                        #  - --attach면 fail-closed(중단)
                        #  - 아니면 pack이 상한 내일 때만 프롬프트에 인라인 붙여 폴백, 초과면 fail-closed(잘린 전송 방지)
                        send_prompt = prompt
                        attachment = None
                        if pack_path is not None:
                            attachment = attach_file(page, pack_path)
                            if attachment["state"] == "confirmed":
                                if not args.no_project:
                                    # 같은 프로젝트의 옛 채팅/파일을 근거로 쓰는 오염 방지(2026-07-19 카운슬 P2)
                                    send_prompt = prompt + PROJECT_SCOPE_GUARD
                            else:
                                state = attachment.get("state")
                                reason = attachment.get("reason")
                                if state not in {"not_attempted", "attempted_unconfirmed"}:
                                    state = "unknown"
                                if reason not in _SAFE_ATTACHMENT_REASONS:
                                    reason = "unclassified"
                                print(f"  ❌ 첨부 준비 중단 (state={state}, reason={reason})", flush=True)
                                if args.attach or attachment["state"] != "not_attempted" or not attachment["fallback_allowed"]:
                                    raise RuntimeError("첨부 미확인/unsupported — 전송 및 인라인 fallback 금지")
                                send_prompt = build_paste_fallback(prompt, pack_path)
                                if send_prompt is None:
                                    raise RuntimeError("코드 첨부 실패 + pack이 커서 붙여넣기 폴백 불가 → 중단(fail-closed)")
                                print(f"  ↩︎  첨부 실패 → pack을 프롬프트에 인라인 붙여넣기 폴백({len(send_prompt):,}자, 상한 내)")

                        # 전송 직전 기준 포착(턴-스코프 결속): fail-closed 카운터 + message-id 집합(id-diff 판정용)
                        base_user = count_msgs_strict(page, USER_MSG_SELECTORS)
                        base_assistant = count_msgs_strict(page, ASSISTANT_MSG_SELECTORS)
                        base_copy = count_msgs_strict(page, COPY_BTN_SELECTORS)
                        base_ids_snapshot = msg_id_set(page)

                        composer = active_composer(page)
                        put_text(page, send_prompt, composer)
                        # 보낼 텍스트 '전체'가 입력창에 들어갔는지 검증 — 아니면 composer 비우고 1회 재입력, 그래도 불일치면 중단
                        # (첨부만/잘린 질문이 전송되어 '오염된 응답'을 성공저장하는 fail-open 차단)
                        if not composer_has_prompt(page, send_prompt, composer):
                            clear_composer(page, composer)
                            put_text(page, send_prompt, composer)
                            if not composer_has_prompt(page, send_prompt, composer):
                                raise RuntimeError("프롬프트가 입력창에 온전히 안 들어감 → 중단(첨부만/잘린 전송 방지, fail-closed)")
                        if args.model:
                            final_verified, final_model = select_model(page, args.model, require_model=args.require_model)
                            if not final_verified or final_model != verified_model_name:
                                raise RuntimeError("전송 직전 모델 변경/미확인")
                        binding.update(
                            baseline_user_ids=sorted(set().union(*(node_ids(n) for n in message_nodes(page, "user")))),
                            baseline_assistant_ids=sorted(set().union(*(node_ids(n) for n in message_nodes(page, "assistant")))),
                            model_verification=verified_model_name, project=project_name,
                            sent_text_sha256=hashlib.sha256(normalize(send_prompt).encode()).hexdigest(),
                            sent_text_fingerprint=message_fingerprint(send_prompt),
                            prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
                            pack_sha256=hashlib.sha256(pack_path.read_bytes()).hexdigest() if pack_path else None,
                            phase="SEND_PENDING")
                        dispatch["reason"] = "checkpoint"
                        save_binding(binding)
                        # An uninstrumented/failed call is conservative; click_send
                        # supplies authoritative pre-activation rejection evidence.
                        dispatch.update(state="ACTIVATION_UNKNOWN", reason="activation")
                        attachment_guard = attachment if attachment and attachment.get("state") == "confirmed" else None
                        click_send(page, send_prompt, composer, dispatch, attachment_guard)
                        manifest_written = False

                        def _persist_binding(url, _sp=send_prompt):
                            nonlocal manifest_written
                            if not manifest_written:
                                binding["chat_url"] = url
                                save_binding(binding)
                                manifest_written = True

                        status, text, conv_url = wait_for_turn_response(
                            page, force_after=args.force_answer_after, max_wait=mw_eff,
                            base_user=base_user, base_assistant=base_assistant,
                            base_copy=base_copy, base_ids=base_ids_snapshot, on_bound=_persist_binding,
                            binding=binding, persist=save_binding, save_response=publish_response)
                        if conv_url:
                            _persist_binding(conv_url)  # 결속 콜백이 못 돈 경로(전달된 URL) 보강 — 멱등
                        if status == "error":
                            surface_error = True
                            break
                        if status == "sent_unknown_location":
                            print("  ⚠️  전송 시도 후 대화 URL 미확인 — 중복 방지를 위해 재전송하지 않고 종료")
                            sent_unknown = True
                            break
                        if status == "quota":
                            print("  ⛔ 사용량 한도 — 재시도 무의미, 중단(재전송 없음)")
                            quota_hit = True
                            break
                        if status == "timeout":
                            print("  ⚠️  타임아웃 — 미완성 응답은 성공저장 안 함(fail-closed)"
                                  + (f" → 다음 시도는 같은 채팅 회수 재시도: {conv_url}" if conv_url else " → 재시도"))
                            continue
                        if status == "ok" and text and text.strip():
                            response = text
                        else:
                            print(f"  ⚠️  응답 비었거나 너무 짧음(status={status}) → 재시도")
                finally:
                    try:
                        page.close()
                    except Exception:
                        pass
            if response:
                break
            print(f"  ⚠️  시도 {attempt}: 응답 비어있음")
        except Exception as exc:
            last_failure = ("응답 회수/검증 실패" if conv_url or binding.get("chat_url") else
                            DISPATCH_REASONS.get(dispatch["reason"], "전송 준비/검증 실패"))
            print(f"  ❌ 실행 단계 실패: {last_failure}", file=sys.stderr, flush=True)
            print(f"     ↳ 사유: {failure_detail(exc)}", file=sys.stderr, flush=True)
            if dispatch["state"] != "NOT_DISPATCHED" and not binding.get("chat_url"):
                sent_unknown = True
            break

    if surface_error:
        sys.exit("❌ 가시적 오류/로그인 표면이 지속되어 회수를 중단했습니다. 자동 재시도·재전송 없음."
                 + recovery_hint(binding, manifest_path))
    if quota_hit:
        sys.exit("❌ ChatGPT 사용량 한도 도달 — 대기·재시도 중단. 한도 해제 후 회수하세요."
                 + recovery_hint(binding, manifest_path))
    if sent_unknown:
        sys.exit("❌ 전송 시도 결과/대화 위치 미확인 — 중복 방지 위해 재전송 안 함.\n"
                 "   자동 재전송하지 않습니다. 제출 여부를 먼저 확인하고, 일치하는 대화가 있을 때만 회수하세요:\n"
                 "   pack_and_ask.py --harvest '<채팅URL>'")
    if not response:
        if not harvest_url and dispatch["state"] == "NOT_DISPATCHED":
            sys.exit(f"❌ 전송 전 실패 — 이 실행은 전송되지 않았습니다 ({last_failure or '검증 미완'})")
        sys.exit("❌ 응답 회수 실패" + recovery_hint(binding, manifest_path))

    # 회수 품질 경고(하드 차단 아님 — 카운슬 합의로 경고 강등): 파일-저장형/단답 응답 의심 패턴
    if len(response) < 500 and re.search(r"저장했습니다|다운로드|sandbox:/", response):
        print("  ⚠️  응답이 짧고 파일-저장형 패턴 포함 — 본문 대신 파일로 저장됐을 수 있음(채팅에서 직접 확인 권장)")

    # 패킹 파일 시크릿 위생: --delete-pack이면 삭제
    if pack_path is not None and args.delete_pack:
        try:
            if Path("/usr/bin/trash").is_file():
                subprocess.run(["/usr/bin/trash", str(pack_path)], check=True, capture_output=True)
                print("  🔒 패킹 파일을 휴지통으로 이동")
            else:
                print("  ⚠️ trash 미지원 — 패킹 파일 보존")
        except (OSError, subprocess.SubprocessError):
            pass

    resp_path = out_dir / f"response_{label}_{run_tag}.md"
    # publish_response already revalidated the bound live page and atomically saved it.
    print(f"\n[완료] 응답 저장: {resp_path}")
    if args.council:
        real_stdout.write(response + "\n")
        real_stdout.flush()
    else:
        print("─" * 50)
        print(response[:800] + ("\n...(생략)" if len(response) > 800 else ""))


def main():
    return _main()


if __name__ == "__main__":
    main()
