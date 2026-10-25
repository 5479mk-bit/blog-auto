"""
render_slides.py
05_이미지_팀원 지침 실행기: 슬라이드 스펙(JSON) -> 카드뉴스 PNG 이미지들
(2026-08-20 디자인 전면 개편 — 사용자가 준 레퍼런스 이미지 9장 기반: 크림 배경 + 네이비 포인트,
 얇은 구분선, 라인 아이콘, 헤더바형 표. 썸네일(title)은 실제 실사 4장 콜라주 + 검은 반투명 레이어 배경 — 2026-10-12부터 Unsplash 미사용.)

사용법:
  python render_slides.py slides.json output_dir/

★ 핵심 원칙: AI 검색/네이버는 이미지 안의 글자를 읽지 못한다. 이미지 텍스트는 반드시 본문에도
  문장으로 이미 존재해야 하고, 이미지는 본문의 요약/눈에 띄는 장치일 뿐 본문을 대체하지 않는다.
  그래서 이미지 텍스트는 항상 짧게 유지한다 (JSON 스펙을 만드는 run_daily_post.py의 책임).

slides.json 형식 (리스트), type별 필드:
  {"type": "title", "heading": "행사명", "sub": "9.19~20 · 난지한강공원", "collage": ["사진1.jpg", "사진2.jpg", "사진3.jpg", "사진4.jpg"]}
    - collage: 배경으로 쓸 실사 파일 경로(최대 4장, 2x2 콜라주). 없으면 네이비 단색 배경
  {"type": "columns", "columns": [{"heading":"...", "body":"..."}, {"heading":"...", "body":"..."}]}  (2~3열)
  {"type": "headertable", "header": ["항목1","항목2"], "rows": [["라벨","값"], ["라벨","값"]]}  (1~2열 헤더 + 라벨/값 행)
  {"type": "icongrid", "items": [{"icon":"calendar","label":"..."}, ...]}  (최대 4개, icon은 아래 ICONS 키 중 하나)
  {"type": "checklist", "quote": "핵심 한 줄", "items": ["...", "..."]}
  {"type": "list", "heading": "...", "items": ["...", "..."]}
  {"type": "steps", "heading": "...", "items": ["...", "..."]}

공통: type이 "title"이 아니면 "after_text" 필드도 가짐(본문 줄 매칭용, naver_draft.py가 사용).
규격: 1080x1080 정사각형 PNG, 바깥 80px 안전 여백 안에만 내용 배치.
"""

import base64
import html as html_lib
import json
import os
import sys
import textwrap
from pathlib import Path

from playwright.sync_api import sync_playwright

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

WIDTH, HEIGHT = 1080, 1080
MARGIN = 80
NAVY = "#1B3A5C"
BG = "#EDEAE3"
INK = "#222222"
GRAY = "#666666"
LINE = "#C9C4B8"

FONTS_DIR = Path(__file__).parent / "fonts"


def _font_b64(filename: str) -> str:
    path = FONTS_DIR / filename
    return base64.b64encode(path.read_bytes()).decode("ascii") if path.exists() else ""


_BLACK_HAN_SANS_B64 = _font_b64("black_han_sans_subset.ttf")
_NOTO_700_B64 = _font_b64("noto_700_subset.ttf")
_NOTO_900_B64 = _font_b64("noto_900_subset.ttf")

FONT_FACES = f"""
@font-face {{ font-family: 'Black Han Sans'; src: url(data:font/ttf;base64,{_BLACK_HAN_SANS_B64}) format('truetype'); font-weight: 400; }}
@font-face {{ font-family: 'Noto Sans KR'; src: url(data:font/ttf;base64,{_NOTO_700_B64}) format('truetype'); font-weight: 700; }}
@font-face {{ font-family: 'Noto Sans KR'; src: url(data:font/ttf;base64,{_NOTO_900_B64}) format('truetype'); font-weight: 900; }}
""" if _BLACK_HAN_SANS_B64 else ""

ICONS = {
    "calendar": '<rect x="3" y="5" width="18" height="16" rx="2"/><line x1="3" y1="10" x2="21" y2="10"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="16" y1="2" x2="16" y2="6"/>',
    "location": '<path d="M12 21s7-6.5 7-12a7 7 0 1 0-14 0c0 5.5 7 12 7 12z"/><circle cx="12" cy="9" r="2.5"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 16 14"/>',
    "ticket": '<path d="M3 8a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v2a2 2 0 0 0 0 4v2a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-2a2 2 0 0 0 0-4V8z"/>',
    "chart": '<line x1="4" y1="20" x2="20" y2="20"/><rect x="6" y="12" width="3" height="8"/><rect x="11" y="8" width="3" height="12"/><rect x="16" y="4" width="3" height="16"/>',
    "users": '<circle cx="9" cy="8" r="3.5"/><path d="M2 20c0-3.5 3-6 7-6s7 2.5 7 6"/><circle cx="18" cy="9" r="2.8"/><path d="M15.5 14.2c2.7.3 4.5 2.3 4.5 5.8"/>',
    "check": '<circle cx="12" cy="12" r="9"/><polyline points="8 12.5 11 15.5 16 9"/>',
    "sparkle": '<path d="M12 2l1.8 6.2L20 10l-6.2 1.8L12 18l-1.8-6.2L4 10l6.2-1.8z"/>',
}


def _icon_svg(name: str, size: int = 44) -> str:
    body = ICONS.get(name, ICONS["sparkle"])
    return (
        f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
        f'stroke="{NAVY}" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">{body}</svg>'
    )


TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  {font_faces}
  * {{ margin: 0; padding: 0; box-sizing: border-box; font-family: 'Noto Sans KR', 'Malgun Gothic', sans-serif; font-weight: 700; }}
  body {{ width: {width}px; height: {height}px; background: {bg}; color: {ink}; }}
  .pad {{ padding: {margin}px; height: 100%; display: flex; flex-direction: column; }}
  .eyebrow {{ font-size: 24px; font-weight: 700; color: {navy}; letter-spacing: 2px; text-transform: uppercase; margin-bottom: 24px; }}
  .divider {{ border: none; border-top: 2px solid {navy}; margin: 28px 0; }}
  .thin-divider {{ border: none; border-top: 1px solid {line}; }}
</style>
</head>
<body>
{content}
</body>
</html>
"""


# ── title (썸네일: 실제 실사 4장 2x2 콜라주 + 검은 반투명 레이어 + 중앙 흰 제목) ──────────
# 2026-10-12 개편 — Unsplash 스톡 사진 로직은 완전히 삭제했다(상관없는 사진이 깔리는 문제).
# 배경은 리서치 단계에서 받은 실제 핫플/팝업 실사만 쓴다. 사진이 4장보다 적으면 있는 사진을 반복 배치하고,
# 한 장도 없으면 네이비 단색 배경으로 만든다.
def _img_data_uri(path: str, size: int = 540) -> str | None:
    """사진 파일을 정사각형(중앙 크롭) size x size JPEG data URI로 만든다. Pillow가 없으면 원본 바이트를 그대로 쓴다."""
    try:
        raw = Path(path).read_bytes()
    except Exception:
        return None
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        w, h = im.size
        m = min(w, h)
        left, top = (w - m) // 2, (h - m) // 2
        im = im.crop((left, top, left + m, top + m)).resize((size, size))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        mime = "image/png" if raw[:4] == b"\x89PNG" else "image/webp" if raw[:4] == b"RIFF" else "image/gif" if raw[:3] == b"GIF" else "image/jpeg"
        return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


def _title_font_px(heading: str) -> int:
    n = len(heading)
    return 120 if n <= 8 else 104 if n <= 12 else 88 if n <= 16 else 74 if n <= 20 else 64


def _render_title(spec: dict) -> str:
    paths = (spec.get("collage") or [])[:4]
    n = len(paths)
    # 사진 1장이면 그 사진을 전체 배경으로(4칸 반복 X), 2장이면 좌우 반반, 3장이면 위 1장(넓게)+아래 2장, 4장이면 2x2.
    uris = [u for u in (_img_data_uri(p, 1080 if n == 1 else 720) for p in paths) if u]
    if uris:
        def tile(u, extra=""):
            return f'<div style="background-image:url({u});background-size:cover;background-position:center;{extra}"></div>'
        if len(uris) == 1:
            bg = f'<div style="position:absolute;inset:0;background-image:url({uris[0]});background-size:cover;background-position:center;"></div>'
        else:
            if len(uris) == 2:
                cols, rows, cells = "1fr 1fr", "1fr", [tile(u) for u in uris]
            elif len(uris) == 3:
                cols, rows, cells = "1fr 1fr", "1fr 1fr", [tile(uris[0], "grid-column:1 / span 2;"), tile(uris[1]), tile(uris[2])]
            else:
                cols, rows, cells = "1fr 1fr", "1fr 1fr", [tile(u) for u in uris[:4]]
            bg = (f'<div style="position:absolute;inset:0;display:grid;grid-template-columns:{cols};grid-template-rows:{rows};gap:4px;'
                  f'background:#fff;">{"".join(cells)}</div>')
    else:
        bg = f'<div style="position:absolute;inset:0;background:{NAVY};"></div>'
    heading = html_lib.escape(spec["heading"])
    sub = html_lib.escape(spec.get("sub", ""))
    fpx = _title_font_px(spec["heading"])
    return f"""
    <div style="position:relative;width:{WIDTH}px;height:{HEIGHT}px;overflow:hidden;">
      {bg}
      <div style="position:absolute;inset:0;background:rgba(0,0,0,.46);"></div>
      <div style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;">
        <div style="text-align:center;padding:0 80px;">
          <div style="font-family:'Black Han Sans','Noto Sans KR',sans-serif;font-weight:400;font-size:{fpx}px;
                      color:#fff;line-height:1.25;word-break:keep-all;letter-spacing:-1px;
                      text-shadow:0 3px 22px rgba(0,0,0,.55);">{heading}</div>
          <div style="margin:34px auto 0;width:120px;border-top:4px solid rgba(255,255,255,.9);"></div>
          <div style="margin-top:30px;font-size:44px;font-weight:900;color:#fff;opacity:.96;word-break:keep-all;
                      text-shadow:0 2px 12px rgba(0,0,0,.55);">{sub}</div>
        </div>
      </div>
    </div>
    """


# ── columns (2~3열, 각 열 제목+본문, 얇은 세로 구분선) ──────────
def _render_columns(spec: dict) -> str:
    cols = spec.get("columns", [])[:3]
    col_html = ""
    for i, col in enumerate(cols):
        border = "" if i == 0 else f"border-left:1px solid {LINE}; padding-left:44px; margin-left:44px;"
        col_html += f"""
        <div style="flex:1; {border}">
          <div style="font-size:42px; font-weight:900; color:{NAVY}; margin-bottom:28px; word-break:keep-all; line-height:1.3;">{col.get('heading','')}</div>
          <div style="font-size:30px; font-weight:700; color:{GRAY}; line-height:1.65; word-break:keep-all;">{col.get('body','')}</div>
        </div>
        """
    return f"""
    <div class="pad">
      <div class="eyebrow">COMPARE</div>
      <hr class="divider">
      <div style="flex:1; display:flex; align-items:center;">{col_html}</div>
    </div>
    """


# ── headertable (네이비 헤더바 + 라벨/값 행) ──────────
# 2026-10-12 개편 — 긴 글자가 칸 밖으로 잘리던 문제: (1) textwrap으로 칸 폭에 맞춰 미리 줄바꿈하고,
# (2) 렌더 후 JS(FIT_TABLE_JS)가 표 전체 높이/칸 폭을 넘으면 글자 크기와 여백을 줄여서 반드시 들어가게 한다.
def _wrap_cell(text: str, width_chars: int) -> str:
    lines = textwrap.wrap(str(text), width=max(6, width_chars), break_long_words=True, break_on_hyphens=False) or [""]
    return "<br>".join(html_lib.escape(l) for l in lines)


def _render_headertable(spec: dict) -> str:
    headers = spec.get("header", [])
    rows = spec.get("rows", [])
    n_lines = 0
    ratios = [1, 2] if len(headers) == 2 else [1] * len(headers)
    total = sum(ratios)
    col_px = [int(920 * r / total) for r in ratios]
    total_text = sum(len(str(c)) for row in rows for c in row)
    base_fs = 32 if total_text <= 120 else 28 if total_text <= 190 else 24 if total_text <= 260 else 20
    char_w = base_fs * 0.98
    header_html = "".join(
        f'<div data-cell style="flex:{r}; background:{NAVY}; color:#fff; font-size:{base_fs + 2}px; font-weight:900; '
        f'padding:{int(base_fs * 0.9)}px 20px; text-align:center; word-break:keep-all;">{html_lib.escape(str(h))}</div>'
        for h, r in zip(headers, ratios)
    )
    rows_html = ""
    for row in rows:
        cells = ""
        for cell, r, px in zip(row, ratios, col_px):
            wrapped = _wrap_cell(cell, int((px - 40) / char_w))
            n_lines += wrapped.count("<br>") + 1
            cells += (f'<div data-cell style="flex:{r}; padding:{int(base_fs * 0.9)}px 20px; font-size:{base_fs}px; color:{INK}; '
                      f'text-align:center; word-break:keep-all; overflow-wrap:anywhere; line-height:1.4;">{wrapped}</div>')
        rows_html += f'<div style="display:flex; align-items:center; border-bottom:1px solid {LINE};">{cells}</div>'
    return f"""
    <div class="pad">
      <div style="flex:1; display:flex; flex-direction:column; justify-content:center;">
        <div data-fit-table data-fs="{base_fs}" style="border:1px solid {LINE};">
          <div style="display:flex;">{header_html}</div>
          {rows_html}
        </div>
      </div>
    </div>
    """


FIT_TABLE_JS = """() => {
  const t = document.querySelector('[data-fit-table]');
  if (!t) return 0;
  const cells = Array.from(t.querySelectorAll('[data-cell]'));
  let fs = parseInt(t.dataset.fs || '32', 10);
  const maxH = 880;
  const over = () => t.getBoundingClientRect().height > maxH || cells.some(c => c.scrollWidth > c.clientWidth + 1);
  let guard = 0;
  while (over() && fs > 15 && guard++ < 30) {
    fs -= 2;
    cells.forEach(c => { c.style.fontSize = fs + 'px'; c.style.padding = Math.max(10, Math.round(fs * 0.7)) + 'px 18px'; });
  }
  return fs;
}"""


# ── icongrid (2x2 아이콘+라벨, 얇은 십자 구분선) ──────────
def _render_icongrid(spec: dict) -> str:
    items = spec.get("items", [])[:4]
    cells = ""
    for i, item in enumerate(items):
        right_border = f"border-right:1px solid {LINE};" if i % 2 == 0 else ""
        bottom_border = f"border-bottom:1px solid {LINE};" if i < 2 else ""
        cells += f"""
        <div style="width:50%; {right_border}{bottom_border} padding:52px 40px; display:flex; align-items:center; gap:26px; box-sizing:border-box;">
          {_icon_svg(item.get('icon', 'sparkle'), 60)}
          <div style="font-size:32px; font-weight:800; color:{INK}; word-break:keep-all; line-height:1.35;">{item.get('label','')}</div>
        </div>
        """
    return f"""
    <div class="pad">
      <div style="flex:1; display:flex; align-items:center;">
        <div style="width:100%; display:flex; flex-wrap:wrap; border:1px solid {LINE};">{cells}</div>
      </div>
    </div>
    """


# ── checklist (중앙 강조 문구 박스 + 체크리스트) ──────────
def _render_checklist(spec: dict) -> str:
    items_html = "".join(
        f'<div style="display:flex; gap:20px; margin-bottom:32px; align-items:flex-start;">'
        f'<div style="flex-shrink:0; margin-top:2px;">{_icon_svg("check", 38)}</div>'
        f'<div style="font-size:32px; font-weight:700; color:{INK}; line-height:1.5; word-break:keep-all;">{item}</div></div>'
        for item in spec.get("items", [])
    )
    return f"""
    <div class="pad">
      <div style="flex:1; display:flex; flex-direction:column; justify-content:center;">
        <div style="background:#fff; border:1px solid {LINE}; padding:56px; margin-bottom:56px;">
          <div style="font-size:42px; font-weight:900; color:{NAVY}; line-height:1.5; word-break:keep-all; text-align:center;">"{spec.get('quote','')}"</div>
        </div>
        <div>{items_html}</div>
      </div>
    </div>
    """


# ── list / steps (기존 유지, 팔레트만 네이비로) ──────────
def _render_list(spec: dict) -> str:
    items_html = "".join(
        f'<li style="counter-increment:item; font-size:36px; font-weight:700; margin-bottom:44px; '
        f'padding-left:72px; position:relative; word-break:keep-all; line-height:1.4;">'
        f'<span style="position:absolute; left:0; top:-4px; width:46px; height:46px; border-radius:50%; '
        f'background:{NAVY}; color:#fff; font-size:26px; font-weight:900; display:flex; align-items:center; '
        f'justify-content:center;">{i+1}</span>{item}</li>'
        for i, item in enumerate(spec.get("items", []))
    )
    return f"""
    <div class="pad">
      <div style="flex:1; display:flex; flex-direction:column; justify-content:center;">
        <div class="eyebrow">{spec.get('heading','')}</div>
        <hr class="divider">
        <ol style="list-style:none;">{items_html}</ol>
      </div>
    </div>
    """


def _render_steps(spec: dict) -> str:
    return _render_list(spec)  # 동일 스타일 재사용 (번호+세로 구분선 이미 있음)


# ── infocard (2026-10-21) — K팝·축제 글용 정보 카드: 공식 포스터를 흐리게 깐 배경 + 반투명 패널 + 라벨/값 행 ──
# 네이버 검색 결과에 글 이미지 썸네일 5장 줄이 노출되면서 이미지 장수·주목도가 클릭률을 좌우(경쟁 글 5~16장).
_CARD_ACCENTS = ["#FF4FA3", "#7C5CFF", "#00B8D9", "#FF8A00", "#22C55E", "#F43F5E"]


def _render_infocard(spec: dict) -> str:
    bg_uri = _img_data_uri(spec["bg"], 1080) if spec.get("bg") else None
    accent = spec.get("accent") or _CARD_ACCENTS[0]
    bg = (f'<div style="position:absolute;inset:-40px;background-image:url({bg_uri});background-size:cover;background-position:center;filter:blur(18px) saturate(1.3);"></div>'
          if bg_uri else f'<div style="position:absolute;inset:0;background:linear-gradient(135deg,#1a1033 0%,{accent} 140%);"></div>')
    rows = spec.get("rows", [])[:7]
    total = sum(len(str(a)) + len(str(b)) for a, b in rows) or 1
    fs = 34 if total <= 140 else 30 if total <= 220 else 26 if total <= 320 else 22
    rows_html = "".join(
        f'<div style="display:flex;gap:26px;padding:{int(fs*0.62)}px 0;border-bottom:1px solid rgba(255,255,255,.18);">'
        f'<div style="flex:0 0 230px;font-size:{fs}px;font-weight:900;color:{accent};word-break:keep-all;">{html_lib.escape(str(a))}</div>'
        f'<div style="flex:1;font-size:{fs}px;font-weight:700;color:#fff;line-height:1.42;word-break:keep-all;overflow-wrap:anywhere;">{_wrap_cell(b, 26)}</div></div>'
        for a, b in rows)
    return f"""
    <div style="position:relative;width:{WIDTH}px;height:{HEIGHT}px;overflow:hidden;">
      {bg}
      <div style="position:absolute;inset:0;background:rgba(10,8,25,.62);"></div>
      <div style="position:absolute;inset:70px;display:flex;flex-direction:column;">
        <div style="align-self:flex-start;background:{accent};color:#fff;font-size:30px;font-weight:900;padding:10px 26px;border-radius:40px;">{html_lib.escape(spec.get('tag',''))}</div>
        <div style="margin-top:26px;font-family:'Black Han Sans','Noto Sans KR',sans-serif;font-size:{70 if len(spec.get('heading','')) <= 12 else 58}px;color:#fff;line-height:1.2;word-break:keep-all;text-shadow:0 3px 18px rgba(0,0,0,.5);">{html_lib.escape(spec.get('heading',''))}</div>
        <div data-fit-table data-fs="{fs}" style="margin-top:70px;flex:1;display:flex;flex-direction:column;justify-content:flex-start;">{rows_html}</div>
        <div style="text-align:right;font-size:22px;color:rgba(255,255,255,.7);font-weight:700;">{html_lib.escape(spec.get('foot',''))}</div>
      </div>
    </div>
    """


RENDERERS = {
    "title": _render_title,
    "columns": _render_columns,
    "headertable": _render_headertable,
    "icongrid": _render_icongrid,
    "checklist": _render_checklist,
    "list": _render_list,
    "steps": _render_steps,
    "infocard": _render_infocard,
}


def render_slides(slides: list[dict], out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": WIDTH, "height": HEIGHT})
        for i, spec in enumerate(slides, 1):
            renderer = RENDERERS.get(spec["type"])
            if renderer is None:
                raise ValueError(f"알 수 없는 슬라이드 type: {spec['type']}")
            content = renderer(spec)
            html = TEMPLATE.format(width=WIDTH, height=HEIGHT, margin=MARGIN, bg=BG, navy=NAVY, ink=INK, line=LINE, font_faces=FONT_FACES, content=content)
            page.set_content(html)
            page.wait_for_timeout(150)
            if spec["type"] == "headertable":
                page.evaluate(FIT_TABLE_JS)
                page.wait_for_timeout(80)
            out_path = out_dir / f"slide_{i}.png"
            page.screenshot(path=str(out_path))
            paths.append(out_path)
        browser.close()
    return paths


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("사용법: python render_slides.py slides.json output_dir/")
        sys.exit(1)

    slides_json = Path(sys.argv[1])
    out_dir = Path(sys.argv[2])
    slides = json.loads(slides_json.read_text(encoding="utf-8"))
    paths = render_slides(slides, out_dir)
    for p in paths:
        print(p)
