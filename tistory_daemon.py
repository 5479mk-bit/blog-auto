#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tistory_daemon.py — 티스토리 상주 브라우저 데몬 (2026-09-26 신설).

티스토리(카카오 계정) 로그인 상태는 쿠키가 아니라 브라우저 세션(Session Storage) 기반이라
Naver처럼 "프로필 폴더 재사용"만으로는 프로세스를 껐다 켜도 로그인이 유지되지 않는다
(tistory_draft.py의 check_login_session() 실측으로 확인됨). 그래서 이 데몬은 브라우저를
한 번 띄운 뒤 절대 닫지 않고, 로컬 HTTP로 글 저장 요청을 받아 그 살아있는 페이지에 그대로
써넣는 방식으로 동작한다.

사용법:
  1) 사용자가 PC 앞에서 `python tistory_daemon.py` 실행 → 브라우저 창이 뜨면 카카오 계정으로
     로그인 (이 창을 그대로 켜두기 — 절대 닫지 말 것). ★ 2026-09-27: 창을 최소화해두면
     크로미움이 백그라운드 탭을 스로틀링해서 본문이 빈 채로 발행되는 사고가 실제로 한 번
     있었다. 스로틀링을 끄는 실행 옵션(_CHROME_LAUNCH_KWARGS)을 추가했지만 장시간 검증은
     안 됐으니, 당분간은 최소화하지 말고 다른 창 뒤에 두는 정도로만 사용 권장.
  2) 로그인이 확인되면 http://127.0.0.1:8765 에서 요청을 받기 시작한다.
  3) run_daily_post.py가 매 실행마다 이 데몬에 POST /post 로 글을 보낸다. 데몬이 꺼져 있으면
     (connection refused) 티스토리 저장은 그냥 건너뛰고 네이버 저장은 그대로 진행한다 — 티스토리
     쪽 장애가 네이버 파이프라인을 막지 않는다.

★★★★★ (2026-09-26 변경 — 사용자 명시적 지시) 원래는 "임시저장"만 하고 발행 버튼은 절대
누르지 않는 게 이 프로젝트 전역 규칙이었다. 사용자가 "티스토리 네이버 둘 다 자동발행으로"라고
명시적으로 요청해서, 검토 없이 실수가 그대로 공개되는 리스크를 알리고 재확인받은 뒤 이 규칙을
해제했다. publish=True로 호출하면 발행 레이어의 확정 버튼([id="publish-btn"], 2026-09-26
실측)까지 눌러 즉시 공개 발행한다.
"""

import json
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from playwright.sync_api import sync_playwright

from tistory_draft import BASE, PROFILE_DIR, WRITE_URL, MANAGE_URL, _CHROME_LAUNCH_KWARGS, _patch_webdriver_flag

HOST, PORT = "127.0.0.1", 8765

# 2026-09-26 실측 — window.Config.blog.categories에서 확인한 실제 카테고리 id.
# "issue" 카테고리가 이미 있어서(과거에 수동으로 만들어둔 것으로 보임) 네이버 쪽
# NAVER_CATEGORY_GOSSIP = "issue" 네이밍과 그대로 맞춰 재사용한다.
TISTORY_CATEGORY_ISSUE = "issue"

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _try_kakao_sso(page) -> None:
    """2026-10-26: 티스토리 자체 세션(TSSESSION)은 금방 끊기지만 카카오 계정 쪽 '로그인 유지'
    쿠키는 훨씬 오래 산다. Actions에선 아무도 '카카오계정으로 로그인' 버튼을 안 눌러서 매번
    로그인 실패로 끝났다 → 버튼(과 카카오 '계속하기' 확인)을 대신 눌러 자동 재로그인시킨다."""
    for label in ("카카오계정으로 로그인", "카카오 계정으로 로그인", "5479mz@naver.com", "장보형", "계속하기", "Continue"):
        try:
            btn = page.get_by_text(label, exact=False).first
            if btn.is_visible(timeout=500):
                btn.click(timeout=3000)
                page.wait_for_timeout(3000)
                return
        except Exception:
            pass


def _login_state(page) -> str:
    """로그인이 안 될 때 원인을 로그로 남기기 위한 현재 화면 요약(주소·제목·보이는 글자 일부). 2026-10-28: 쿠키 복원 후 몇 시간 만에
    로그인이 실패했는데 로그에 아무것도 없어 원인을 알 수 없었다(캡차/2단계 인증/새 환경 로그인 확인 등일 수 있음)."""
    try:
        text = re.sub(r"\s+", " ", page.inner_text("body", timeout=2000))[:260]
    except Exception:
        text = "(본문 읽기 실패)"
    try:
        title = page.title()
    except Exception:
        title = ""
    return f"url={page.url[:140]} | title={title[:40]} | text={text}"


def _wait_for_login(page, timeout_sec: int = 600) -> bool:
    deadline = time.time() + timeout_sec
    stable_since = None
    next_report = time.time() + 20
    while time.time() < deadline:
        url = page.url
        if time.time() >= next_report:
            print(f"[tistory-daemon] 로그인 대기 중 — {_login_state(page)}", flush=True)
            next_report = time.time() + 45
        looks_logged_in = "/manage" in url and "login" not in url and "kakao" not in url
        if not looks_logged_in:
            _try_kakao_sso(page)
        if looks_logged_in:
            if stable_since is None:
                stable_since = time.time()
            elif time.time() - stable_since > 5:
                return True
        else:
            stable_since = None
        page.wait_for_timeout(1500)
    return False


_CATEGORY_JS = r"""(name) => {
    const norm = s => (s || '').replace(/\s+/g, '').replace(/[\(\[]\d+[\)\]]$/, '');
    const want = norm(name);
    const list = document.querySelector('#category-list');
    if (!list) return false;
    const items = Array.from(list.querySelectorAll('*')).filter(e => e.children.length === 0 || e.matches('li, [role=option], [category-id]'));
    const hit = items.find(e => norm(e.textContent) === want) || items.find(e => norm(e.textContent).includes(want));
    if (!hit) return false;
    (hit.closest('[category-id], li, [role=option]') || hit).click();
    return true;
}"""


def _select_category(page, category_name: str) -> None:
    """2026-10-26: 카테고리가 '카테고리 없음'으로 발행된 사고 대응 — 정확 일치 클릭 → JS 클릭 순으로 시도하고,
    선택 후 버튼 라벨이 실제로 바뀌었는지 검증한다. 못 맞추면 예외(발행하지 않게 호출부에서 중단)."""
    want = category_name.replace(" ", "")
    last_err = None
    for attempt in range(3):
        try:
            page.click("#category-btn")
            page.wait_for_selector("#category-list", timeout=5000)
            page.wait_for_timeout(500)
            try:
                page.locator("#category-list").get_by_text(category_name, exact=True).first.click(timeout=3000)
            except Exception:
                if not page.evaluate(_CATEGORY_JS, category_name):
                    raise RuntimeError(f"카테고리 목록에서 '{category_name}'을(를) 찾지 못함")
            page.wait_for_timeout(500)
            label = (page.inner_text("#category-btn") or "").replace(" ", "")
            if want in label:
                return
            last_err = RuntimeError(f"선택 후 라벨이 '{label}' (기대: '{category_name}')")
        except Exception as e:
            last_err = e
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)
    raise RuntimeError(f"카테고리 선택 실패: {last_err}")


# 2026-10-11 — 태그 입력 실패(`waiting for locator("#tagText")` 30초 타임아웃) 대응: 에디터 UI가 바뀌어
# id가 달라지거나 입력창이 가려져도 되도록 (1) 여러 셀렉터를 순서대로 시도하고, (2) 그래도 못 쓰면
# JS로 입력창을 찾아 네이티브 setter로 값을 넣고 input/keydown(Enter)/keyup 이벤트를 강제로 발화한다.
# (티스토리 Open API는 서비스가 종료돼 쓸 수 없어서 UI 쪽을 견고하게 만드는 방식을 택했다.)
_TAG_SELECTORS = ("#tagText", "input[name='tagText']", "input.tf_tag", "input[placeholder*='태그']")

_TAG_JS = r"""(tags) => {
    const cands = Array.from(document.querySelectorAll('input[type=text], input:not([type])'));
    const input = document.querySelector('#tagText') || cands.find(e =>
        /태그|tag/i.test((e.id || '') + (e.name || '') + (e.placeholder || '') + (e.className || '')));
    if (!input) return 0;
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
    let n = 0;
    for (const t of tags) {
        input.focus();
        setter.call(input, t);
        input.dispatchEvent(new Event('input', {bubbles: true}));
        for (const type of ['keydown', 'keypress', 'keyup']) {
            input.dispatchEvent(new KeyboardEvent(type, {key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true}));
        }
        n++;
    }
    return n;
}"""


def _set_tags(page, tags: list[str]) -> None:
    for sel in _TAG_SELECTORS:
        loc = page.locator(sel).first
        try:
            loc.wait_for(state="attached", timeout=4000)
            loc.scroll_into_view_if_needed(timeout=3000)
            for t in tags:
                loc.click(timeout=3000)
                loc.fill("")
                loc.type(t)
                page.keyboard.press("Enter")
                page.wait_for_timeout(150)
            return
        except Exception as e:
            print(f"[tistory] 태그 입력창 {sel!r} 실패 → 다음 방법 시도: {type(e).__name__}", file=sys.stderr)
    n = page.evaluate(_TAG_JS, tags)
    if not n:
        raise RuntimeError("태그 입력창을 찾지 못함(셀렉터 4종 + JS 모두 실패)")
    print(f"[tistory] 태그 {n}개를 JS 이벤트 발화로 입력", flush=True)


# 2026-09-27 — 사용자 제안으로 방식 전환: 툴바의 이미지 버튼 id가 TinyMCE 로드마다 자동
# 재생성돼서(mceu_0, mceu_1, ...) 고정 셀렉터로 못 찾는 문제가 있었다("사진은 그냥 복붙하면
# 될텐데" — 정확한 지적). 사람이 이미지를 복사해서 Ctrl+V로 붙여넣는 것과 똑같이, 합성
# ClipboardEvent에 이미지 파일을 담아 에디터에 dispatch한다. 툴바 버튼/메뉴를 전혀 거치지
# 않아서 훨씬 안정적이고(실측 확인: 이미지가 카카오 CDN에 자동 업로드되고 <img>로 삽입됨),
# TinyMCE 버전이 바뀌어도 잘 안 깨진다.
_PASTE_IMAGE_JS = r"""(b64) => {
    const ed = window.tinymce.get('editor-tistory');
    const body = ed.getBody();
    const byteChars = atob(b64);
    const bytes = new Uint8Array(byteChars.length);
    for (let i = 0; i < byteChars.length; i++) bytes[i] = byteChars.charCodeAt(i);
    // 움짤(GIF)이 PNG로 위장돼 정지 이미지가 되지 않도록 base64 머리말로 실제 형식을 판별한다.
    const kinds = [['R0lGOD', 'image/gif', 'gif'], ['/9j/', 'image/jpeg', 'jpg'], ['UklGR', 'image/webp', 'webp']];
    const hit = kinds.find(k => b64.startsWith(k[0])) || ['', 'image/png', 'png'];
    const file = new File([bytes], 'image.' + hit[2], {type: hit[1]});
    const dt = new DataTransfer();
    dt.items.add(file);
    ed.focus();
    const evt = new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true});
    body.dispatchEvent(evt);
    return true;
}"""

_PLACE_CURSOR_JS = r"""(args) => {
    const [anchor, lead] = args;
    const ed = window.tinymce.get('editor-tistory');
    const body = ed.getBody();
    const compact = s => (s || '').replace(/[\s:ㆍ>]/g, '');
    let target = null;
    if (anchor) {
        const key = compact(anchor);
        for (const el of body.querySelectorAll('p,h2,h3,blockquote,li,tr')) {
            if (key && compact(el.textContent) === key) { target = el.closest('table,ul,blockquote') || el; break; }
        }
    }
    const p = ed.dom.create('p', {}, '<br data-mce-bogus="1">');
    if (target) ed.dom.insertAfter(p, target);
    else if (lead && body.firstChild) body.insertBefore(p, body.firstChild);
    else body.appendChild(p);
    ed.focus();
    ed.selection.setCursorLocation(p, 0);
    return !!target;
}"""

_IMG_COUNT_JS = "() => window.tinymce.get('editor-tistory').getBody().querySelectorAll('img').length"

# 2026-10-12 — 사진 출처 캡션: 방금 붙인 이미지(아직 data-seen 표시가 없는 img) 바로 아래에 가운데 정렬·12px·회색 문단으로 붙인다.
# (본문 문장에 섞이지 않고 이미지 하단 캡션 자리에 오도록 이미지 블록 바로 다음 형제로 삽입)
_MARK_SEEN_JS = """() => { window.tinymce.get('editor-tistory').getBody().querySelectorAll('img').forEach(i => i.setAttribute('data-seen', '1')); }"""

_CAPTION_JS = r"""(caption) => {
    const ed = window.tinymce.get('editor-tistory');
    const body = ed.getBody();
    const img = Array.from(body.querySelectorAll('img')).find(i => !i.hasAttribute('data-seen'));
    if (!img) return false;
    let node = img;
    while (node.parentNode && node.parentNode !== body) node = node.parentNode;
    const p = ed.dom.create('p', {style: 'text-align: center; font-size: 12px; color: #888888; margin-top: 4px;'}, '');
    p.textContent = caption;
    node.parentNode.insertBefore(p, node.nextSibling);
    body.querySelectorAll('img').forEach(i => i.setAttribute('data-seen', '1'));
    return true;
}"""

# 2026-09-27 실측: #attach-layer-btn이 DOM에 2~3개 중복 렌더링되고(반응형 툴바 clone), 실제
# 보이는 건 그중 하나뿐이라 Playwright의 보통 click()/force click 둘 다 "Element is not
# visible"로 실패한다. offsetParent(화면에 실제로 그려지는지)로 걸러서 진짜 보이는 요소에만
# 네이티브 click()을 직접 디스패치한다 — TinyMCE는 실제 click 이벤트를 듣고 있어서 이렇게
# 해도 메뉴가 정상적으로 열린다.
_CLICK_VISIBLE_JS = r"""(id) => {
    const els = Array.from(document.querySelectorAll('#' + id));
    const visible = els.find(e => {
        const r = e.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    });
    if (!visible) return false;
    visible.click();
    return true;
}"""


def _insert_images(page, images: list[dict]) -> int:
    """본문을 넣은 뒤 사진을 한 장씩 클립보드 붙여넣기 방식으로 올린다 (2026-09-27 신설 —
    예전엔 티스토리 글에 사진이 0장이었다). 붙일 위치(after_text) 바로 뒤에 빈 문단을 만들고
    커서를 둔 다음, 합성 paste 이벤트로 이미지 파일을 넣는다(_PASTE_IMAGE_JS 참고 — 툴바
    버튼을 전혀 안 거쳐서 TinyMCE 버전 변화에 강하다). 같은 자리에 여러 장이면 역순으로
    넣어야 원래 순서가 된다. 한 장 실패해도 나머지는 계속한다."""
    import base64
    inserted = 0
    for img in reversed(images):
        path = img.get("path")
        if not path or not Path(path).exists():
            continue
        anchor = (img.get("after_text") or "").strip()
        try:
            before = page.evaluate(_IMG_COUNT_JS)
            page.evaluate(_MARK_SEEN_JS)
            page.evaluate(_PLACE_CURSOR_JS, [anchor, not anchor])
            b64 = base64.b64encode(Path(path).read_bytes()).decode("ascii")
            page.evaluate(_PASTE_IMAGE_JS, b64)
            deadline = time.time() + 30
            while time.time() < deadline and page.evaluate(_IMG_COUNT_JS) <= before:
                page.wait_for_timeout(500)
            if page.evaluate(_IMG_COUNT_JS) > before:
                inserted += 1
                caption = (img.get("caption") or "").strip()
                if caption:
                    page.wait_for_timeout(600)  # 업로드 후 편집기가 img 요소를 교체할 수 있어 잠깐 기다린다
                    try:
                        page.evaluate(_CAPTION_JS, caption[:80])
                    except Exception as e:
                        print(f"[tistory] 캡션 삽입 실패(건너뜀): {type(e).__name__}", file=sys.stderr)
            else:
                print(f"[tistory] 사진 업로드 확인 안 됨(건너뜀): {path}", file=sys.stderr)
        except Exception as e:
            print(f"[tistory] 사진 삽입 실패(건너뜀) {Path(path).name}: {e}", file=sys.stderr)
    print(f"[tistory] 사진 삽입 {inserted}/{len(images)}장", flush=True)
    return inserted


def _save_draft(page, title: str, body_html: str, tags: list[str], category: str, publish: bool = False,
                images: list[dict] | None = None) -> tuple[str, int]:
    """글쓰기 페이지에 제목/본문/카테고리/태그를 채우고 저장한다.
    publish=True면 발행 레이어를 열어 확정 버튼([id="publish-btn"], 2026-09-26 실측)까지
    눌러 즉시 공개 발행한다 (기본 공개설정이 이미 "공개"라 별도 옵션 변경 불필요).
    publish=False(기본값)면 예전과 동일하게 "임시저장"만 한다.
    반환: 저장/발행 후 URL."""
    page.goto(WRITE_URL, timeout=30000)
    page.wait_for_selector("#post-title-inp", timeout=20000)
    page.fill("#post-title-inp", title)
    if category:
        _select_category(page, category)  # 실패하면 예외 → '카테고리 없음'으로 공개 발행되지 않게 중단
    # 2026-09-27 추가 — 실측 버그: TinyMCE가 초기화되기 전에 setContent를 호출하면 조용히
    # 무시돼서 본문이 빈 채로 발행되는 사고가 실제로 있었다(창을 최소화해서 크로미움이
    # 백그라운드 탭을 스로틀링한 게 원인으로 추정 — 위 _CHROME_LAUNCH_KWARGS에서 스로틀링
    # 자체를 껐지만, 그래도 에디터 준비를 명시적으로 기다리고 결과까지 검증한다).
    page.wait_for_function(
        "() => window.tinymce && window.tinymce.get('editor-tistory') "
        "&& window.tinymce.get('editor-tistory').initialized",
        timeout=20000,
    )
    # TinyMCE 인스턴스에 직접 HTML을 넣는다 — CodeMirror/WYSIWYG iframe을 직접 조작하는 것보다
    # 훨씬 안정적이다 (2026-09-26 실측: #editor-tistory가 TinyMCE의 백업 textarea).
    page.evaluate(
        "html => window.tinymce.get('editor-tistory').setContent(html)", body_html
    )
    page.wait_for_timeout(500)
    # 실제로 반영됐는지 읽어서 검증 — 비어 있으면 한 번 더 시도하고, 그래도 비어 있으면
    # 저장/발행을 중단한다(빈 본문이 그대로 공개 발행되는 사고를 막기 위한 안전장치).
    actual = page.evaluate("() => window.tinymce.get('editor-tistory').getContent()")
    if not actual or not actual.strip():
        print("[tistory] 본문 반영 확인 실패 — 재시도", file=sys.stderr)
        page.wait_for_timeout(1000)
        page.evaluate(
            "html => window.tinymce.get('editor-tistory').setContent(html)", body_html
        )
        page.wait_for_timeout(500)
        actual = page.evaluate("() => window.tinymce.get('editor-tistory').getContent()")
        if not actual or not actual.strip():
            raise RuntimeError("본문이 에디터에 반영되지 않음 — 빈 글 저장/발행을 막기 위해 중단")
    images_inserted = 0
    if images:
        images_inserted = _insert_images(page, images)
        # 사진 넣다가 본문이 날아가는 일이 없었는지 한 번 더 확인한다.
        text_len = page.evaluate("() => window.tinymce.get('editor-tistory').getBody().innerText.trim().length")
        if text_len < 200:
            raise RuntimeError(f"사진 삽입 후 본문 글자 수가 비정상({text_len}자) — 발행 중단")
    if tags:
        try:
            _set_tags(page, tags)
        except Exception as e:
            print(f"[tistory] 태그 입력 실패(건너뜀): {e}", file=sys.stderr)
    page.wait_for_timeout(500)
    if publish:
        page.click("#publish-layer-btn", timeout=10000)
        page.wait_for_selector("#publish-btn", timeout=8000)
        page.click("#publish-btn", timeout=8000)
        page.wait_for_timeout(2500)
        # 2026-09-26 실측: 발행 성공 시 /manage/posts/ (글 목록)로 이동한다 — 개별 글 URL로
        # 리다이렉트되지 않는다. newpost 페이지에 그대로 머물러 있으면(취소되거나 실패) 이상
        # 신호로 본다.
        if "/manage/newpost" in page.url:
            print(f"[tistory] 발행 버튼은 눌렀으나 여전히 글쓰기 페이지에 있음 (현재: {page.url}) "
                  "— 실제 발행 여부 확인 필요", file=sys.stderr)
    else:
        page.click(".wrap_btn .btn-draft a.action", timeout=10000)
        page.wait_for_timeout(2500)
    return page.url, images_inserted


def _debug_dump(page, payload: dict) -> dict:
    """(로컬 전용 조사 도구) 글쓰기 페이지를 열고 지정한 요소들을 차례로 클릭한 뒤 HTML을
    파일로 남긴다 — 에디터 DOM이 바뀌었을 때 데몬을 재시작(=재로그인)하지 않고 셀렉터를
    확인하려는 용도. 발행/저장 버튼은 절대 누르지 않는다."""
    forbidden = ("publish", "btn-draft", "save")
    if payload.get("goto_write"):
        page.goto(WRITE_URL, timeout=30000)
        page.wait_for_selector("#post-title-inp", timeout=20000)
    clicked = []
    for sel in payload.get("clicks") or []:
        if any(f in sel for f in forbidden):
            clicked.append(f"SKIP(forbidden) {sel}")
            continue
        try:
            page.click(sel, timeout=3000, force=True)
            page.wait_for_timeout(600)
            clicked.append(f"OK {sel}")
        except Exception as e:
            clicked.append(f"FAIL {sel}: {type(e).__name__}: {e}")
    for eid in payload.get("click_visible_id") or []:
        if any(f in eid for f in forbidden):
            clicked.append(f"SKIP(forbidden) id={eid}")
            continue
        ok = page.evaluate(_CLICK_VISIBLE_JS, eid)
        page.wait_for_timeout(600)
        clicked.append(f"{'OK' if ok else 'FAIL(not found)'} click_visible_id={eid}")
    out = BASE / "logs" / "tistory_debug_dump.html"
    out.write_text(page.content(), encoding="utf-8")
    file_inputs = page.evaluate(
        "() => Array.from(document.querySelectorAll('input[type=file]')).map(e => e.outerHTML.slice(0, 200))"
    )
    result = {"ok": True, "url": page.url, "clicked": clicked, "file_inputs": file_inputs, "dump": str(out)}
    # 로컬 조사 전용 — 코드 수정 없이 셀렉터를 빠르게 검증하려고 임의 JS 실행을 허용한다
    # (재로그인 없이 반복 확인하려는 목적, 발행/저장 버튼 관련 문자열은 위에서 이미 걸러짐).
    js = payload.get("eval")
    if js and not any(f in js for f in forbidden):
        try:
            result["eval_result"] = page.evaluate(js)
        except Exception as e:
            result["eval_error"] = f"{type(e).__name__}: {e}"
    return result


def main() -> int:
    PROFILE_DIR.mkdir(exist_ok=True)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(PROFILE_DIR), headless=False, viewport={"width": 1280, "height": 900},
            **_CHROME_LAUNCH_KWARGS,
        )
        _patch_webdriver_flag(context)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(MANAGE_URL, timeout=30000)
        print("[tistory-daemon] 로그인 확인 중 — 로그인 안 돼 있으면 브라우저 창에서 카카오 "
              "계정으로 로그인해 주세요 (최대 10분 대기).", flush=True)
        if not _wait_for_login(page):
            print("[tistory-daemon] 로그인 감지 실패 — 종료", file=sys.stderr)
            context.close()
            return 1
        print("[tistory-daemon] 로그인 확인됨 — 이 브라우저 창을 절대 닫지 마세요 "
              "(최소화는 당분간 비권장 — 다른 창 뒤에 두는 정도로). 서버 시작.", flush=True)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                print(f"[tistory-daemon] {fmt % args}", flush=True)

            def do_GET(self):
                if self.path == "/health":
                    self._json(200, {"ok": True})
                elif self.path == "/cookies":  # 2026-10-25: 클라우드(GitHub Actions)로 로그인 세션 쿠키를 옮기기 위한 로컬 전용 엔드포인트
                    import base64, gzip
                    ck = [c for c in context.cookies() if any(dm in c.get("domain", "") for dm in ("tistory.com", "kakao.com", "daum.net"))]
                    self._json(200, {"ok": True, "n": len(ck), "b64": base64.b64encode(gzip.compress(json.dumps(ck).encode())).decode()})
                else:
                    self._json(404, {"ok": False, "error": "not found"})

            def do_POST(self):
                if self.path not in ("/post", "/debug"):
                    self._json(404, {"ok": False, "error": "not found"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", 0))
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                    if self.path == "/debug":
                        self._json(200, _debug_dump(page, payload))
                        return
                    title = payload["title"]
                    body_html = payload["body_html"]
                    tags = payload.get("tags") or []
                    category = payload.get("category") or TISTORY_CATEGORY_ISSUE
                    publish = bool(payload.get("publish"))
                    final_url, n_img = _save_draft(
                        page, title, body_html, tags, category, publish=publish,
                        images=payload.get("images") or [],
                    )
                    action = "발행" if publish else "임시저장"
                    print(f"[tistory-daemon] {action} 완료: {title!r} -> {final_url} (사진 {n_img}장)", flush=True)
                    self._json(200, {"ok": True, "url": final_url, "images_inserted": n_img})
                except Exception as e:
                    print(f"[tistory-daemon] 저장 실패: {e}", file=sys.stderr, flush=True)
                    self._json(500, {"ok": False, "error": str(e)})

            def _json(self, code: int, obj: dict):
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = HTTPServer((HOST, PORT), Handler)
        print(f"[tistory-daemon] http://{HOST}:{PORT} 에서 대기 중 (Ctrl+C로 종료)", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            context.close()
        return 0


if __name__ == "__main__":
    sys.exit(main())
