#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""네이버 카페 게시글의 첨부 영상을 원본 화질로 일괄 저장한다.

화면 녹화가 아니라 서버가 내려주는 원본 스트림을 직접 받으므로
15분짜리 영상을 15분 기다릴 필요가 없다.

    python scripts/naver_cafe_dl.py login          # 최초 1회 (로그인)
    python scripts/naver_cafe_dl.py grab 869       # 글 번호만으로 실행
    python scripts/naver_cafe_dl.py grab <URL>     # 전체 URL 도 가능

옵션:
    --list        내려받지 않고 감지된 영상 목록만 출력
    --debug       진단 정보를 _debug/ 에 남김
    --jobs N      동시 다운로드 수 (기본값: 4)
    --out DIR     저장 폴더 (기본값: 바탕화면\네이버카페영상)
    --cafe ID     카페 ID (기본값: 31568077)

로그인 세션은 저장소가 아니라 사용자 설정 폴더에 보관한다.
쿠키가 커밋되는 일이 없도록 하기 위함이다.
"""

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
import re
import shutil
import subprocess
import sys
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

# Windows 콘솔은 기본 인코딩이 cp949 라 한글 캡션 출력에서 죽는다
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_CAFE = "31568077"
ARTICLE_URL = "https://cafe.naver.com/f-e/cafes/{cafe}/articles/{article}?boardtype=L"
PLAY_API = "https://apis.naver.com/rmcnmv/rmcnmv/vod/play/v2.0/{vid}?key={inkey}"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def desktop_dir():
    """바탕화면 경로. OneDrive 로 옮겨진 한국어 Windows 도 처리한다."""
    home = Path.home()
    for cand in (home / "Desktop", home / "OneDrive" / "Desktop",
                 home / "OneDrive" / "바탕 화면", home / "바탕 화면"):
        if cand.is_dir():
            return cand
    return home / "Desktop"


DEFAULT_OUT = desktop_dir() / "네이버카페영상"


def state_path():
    """로그인 쿠키 저장 위치. 저장소 바깥이어야 한다."""
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "naver_cafe_dl"
    else:
        base = Path.home() / ".config" / "naver_cafe_dl"
    base.mkdir(parents=True, exist_ok=True)
    return base / "state.json"


def launch(p):
    """설치된 Chromium 을 띄운다. 버전이 어긋나면 명확히 알려준다."""
    try:
        return p.chromium.launch(headless=False)
    except Exception as e:
        sys.exit(f"브라우저를 띄우지 못했습니다: {e}\n"
                 f"해결: playwright install chromium")


# ── 로그인 ────────────────────────────────────────────────────────────────
def login():
    state = state_path()
    with sync_playwright() as p:
        browser = launch(p)
        ctx = browser.new_context(user_agent=UA)
        page = ctx.new_page()
        page.goto("https://nid.naver.com/nidlogin.login")
        input("\n>> 브라우저에서 네이버 로그인을 마친 뒤 여기서 Enter... ")
        if not any(c["name"] == "NID_AUT" for c in ctx.cookies()):
            print("경고: 로그인 쿠키가 없습니다. 로그인이 안 된 상태일 수 있습니다.")
        ctx.storage_state(path=str(state))
        browser.close()
    print(f"로그인 상태 저장 완료 -> {state}")


# ── 게시글에서 영상 정보 수집 ──────────────────────────────────────────────
CAPTION_JS = """(node) => {
    // 플레이어 UI 에서 새어 나오는 문자열("100%", "0:00 / 15:00", "재생 173")은 캡션이 아니다
    const junk = /^(\\d+%|[\\d:]+\\s*\\/\\s*[\\d:]+|재생\\s*\\d+|HD|\\d+p)$/;
    const clean = (t) => {
        for (const line of (t || '').split('\\n')) {
            const s = line.trim();
            if (s && !junk.test(s)) return s.slice(0, 80);
        }
        return '';
    };
    const comp = node.closest('.se-component') || node;
    // 1순위: 스마트에디터가 영상 컴포넌트 안에 넣는 캡션
    const cap = comp.querySelector('.se-caption, [class*="caption"]');
    if (cap) { const c = clean(cap.innerText); if (c) return c; }
    // 2순위: 다음 형제 블록의 첫 텍스트
    let n = comp;
    for (let i = 0; i < 5 && n; i++) {
        n = n.nextElementSibling;
        if (!n) break;
        const c = clean(n.innerText);
        if (c) return c;
    }
    return '';
}"""


def collect_from_dom(page):
    """스마트에디터 영상 모듈에서 vid / inkey 를 뽑는다."""
    found = []
    for frame in page.frames:
        try:
            elements = frame.query_selector_all("[data-module]")
        except Exception:
            continue
        for el in elements:
            raw = el.get_attribute("data-module") or ""
            if "video" not in raw:
                continue
            try:
                data = json.loads(raw).get("data") or {}
            except json.JSONDecodeError:
                continue
            vid = data.get("vid") or data.get("videoId")
            inkey = data.get("inkey") or data.get("inKey")
            if not (vid and inkey):
                continue
            try:
                caption = el.evaluate(CAPTION_JS) or ""
            except Exception:
                caption = ""
            found.append({"vid": vid, "inkey": inkey, "title": caption})
    return found


def dump_debug(page, sniffed):
    """영상을 못 찾았을 때 원인 파악용 정보를 남긴다."""
    d = Path("_debug")
    d.mkdir(exist_ok=True)
    modules = []
    for frame in page.frames:
        try:
            for el in frame.query_selector_all("[data-module]"):
                modules.append(el.get_attribute("data-module"))
        except Exception:
            continue
    (d / "modules.json").write_text(json.dumps(modules, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
    (d / "sniffed.json").write_text(json.dumps(sniffed, ensure_ascii=False, indent=2)[:200_000],
                                    encoding="utf-8")
    try:
        page.screenshot(path=str(d / "screen.png"))
    except Exception:
        pass
    (d / "page.html").write_text(page.content()[:2_000_000], encoding="utf-8")
    print(f"\n진단 정보 저장: {d.resolve()}  (modules.json 이 핵심)")


def open_article(url, debug=False):
    state = state_path()
    if not state.exists():
        sys.exit("먼저 `python scripts/naver_cafe_dl.py login` 을 실행하세요.")

    sniffed = []
    with sync_playwright() as p:
        browser = launch(p)
        ctx = browser.new_context(storage_state=str(state), user_agent=UA)
        page = ctx.new_page()

        def on_response(resp):
            if "/rmcnmv/" in resp.url and "vod/play" in resp.url:
                try:
                    sniffed.append(resp.json())
                except Exception:
                    pass

        page.on("response", on_response)
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(3_000)

        # 지연 로딩되는 영상까지 끌어오기 위해 끝까지 스크롤
        for _ in range(30):
            page.mouse.wheel(0, 2_000)
            page.wait_for_timeout(300)
        page.wait_for_timeout(2_000)

        items = collect_from_dom(page)
        print(f"영상 블록 {len(items)}개, 재생 API 응답 {len(sniffed)}건 감지")
        if debug or not (items or sniffed):
            dump_debug(page, sniffed)
        browser.close()

    return items, sniffed


# ── 원본 주소 해석 ────────────────────────────────────────────────────────
def best_source(payload):
    """여러 화질 중 가장 높은 것을 고른다. MP4 가 없으면 HLS 로 폴백."""
    videos = (payload.get("videos") or {}).get("list") or []
    if videos:
        best = max(videos, key=lambda v: (v.get("size") or 0,
                                          (v.get("bitrate") or {}).get("video") or 0))
        return best.get("source")
    for s in payload.get("streams") or []:
        if s.get("source"):
            return s["source"]
    return None


def resolve(item):
    r = requests.get(PLAY_API.format(vid=item["vid"], inkey=item["inkey"]),
                     headers={"User-Agent": UA, "Referer": "https://cafe.naver.com/"},
                     timeout=30)
    r.raise_for_status()
    return best_source(r.json())


def safe_name(text, index):
    text = re.sub(r'[\\/:*?"<>|]', "_", (text or "").strip())
    return f"{index:02d}_{text}" if text else f"{index:02d}_video"


# ── 다운로드 ──────────────────────────────────────────────────────────────
def remote_size(src):
    try:
        r = requests.head(src, headers={"User-Agent": UA}, allow_redirects=True, timeout=30)
        return int(r.headers.get("Content-Length") or 0)
    except Exception:
        return 0


def download(src, dest):
    """받는 동안은 .part 로 두고, 다 받은 뒤에만 최종 이름으로 바꾼다.
    중간에 끊긴 파일이 완성본으로 오인되는 일을 막기 위함이다."""
    part = dest.with_name(dest.name + ".part")
    if ".m3u8" in src:
        if not shutil.which("ffmpeg"):
            raise RuntimeError("HLS 영상은 ffmpeg 가 필요합니다")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error",
                        "-user_agent", UA, "-i", src, "-c", "copy", str(part)], check=True)
    else:
        with requests.get(src, headers={"User-Agent": UA}, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(part, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
    part.replace(dest)
    return dest.stat().st_size


def fetch_one(idx, total, name, src, out):
    dest = out / (name + ".mp4")
    if dest.exists():
        expected = remote_size(src)
        if expected and dest.stat().st_size == expected:
            return f"[{idx}/{total}] {dest.name} — 이미 있음, 건너뜀"
        dest.unlink()  # 크기가 다르면 끊긴 파일이므로 다시 받는다
    print(f"[{idx}/{total}] 시작  {dest.name}", flush=True)
    try:
        size = download(src, dest)
        return f"[{idx}/{total}] 완료  {dest.name}  ({size >> 20}MB)"
    except Exception as e:
        return f"[{idx}/{total}] 실패  {dest.name}  ({e})"


def grab(target, cafe=DEFAULT_CAFE, outdir=DEFAULT_OUT, list_only=False, debug=False, jobs=4):
    url = target if target.startswith("http") else ARTICLE_URL.format(cafe=cafe, article=target)
    print(f"대상: {url}\n")

    items, sniffed = open_article(url, debug=debug)

    sources = []
    for i, it in enumerate(items, 1):
        try:
            src = resolve(it)
        except Exception as e:
            print(f"[{i}] 주소 확인 실패: {e}")
            continue
        if src:
            sources.append((safe_name(it["title"], i), src))

    if not sources:  # DOM 파싱이 실패하면 가로챈 응답을 대신 사용
        for i, payload in enumerate(sniffed, 1):
            src = best_source(payload)
            if src:
                sources.append((safe_name("", i), src))

    if not sources:
        sys.exit("영상을 찾지 못했습니다. 로그인이 풀렸거나 페이지 구조가 다를 수 있습니다.\n"
                 "_debug/modules.json 을 확인하세요.")

    print(f"\n영상 {len(sources)}개 확인\n")
    for name, src in sources:
        print(f"  {name}  <-  {src[:90]}...")
    if list_only:
        return

    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    total = len(sources)
    print(f"동시 {jobs}개씩 다운로드\n")
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = [pool.submit(fetch_one, i, total, name, src, out)
                   for i, (name, src) in enumerate(sources, 1)]
        for fut in as_completed(futures):
            print(fut.result(), flush=True)

    print(f"\n완료 -> {out.resolve()}")


# ── 진입점 ────────────────────────────────────────────────────────────────
def main():
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)

    def opt(flag, default=None):
        return args[args.index(flag) + 1] if flag in args and args.index(flag) + 1 < len(args) else default

    if args[0] == "login":
        login()
    elif args[0] == "grab" and len(args) >= 2:
        grab(args[1],
             cafe=opt("--cafe", DEFAULT_CAFE),
             outdir=opt("--out", DEFAULT_OUT),
             list_only="--list" in args,
             debug="--debug" in args,
             jobs=int(opt("--jobs", 4)))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
