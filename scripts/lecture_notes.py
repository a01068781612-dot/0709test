#!/usr/bin/env python3
"""[4] 결합 — 자막(srt)과 프레임(jpg)을 시간순으로 엮어 강의 노트를 만든다.

  python lecture_notes.py                  # output/자막 + output/프레임 → output/노트
  python lecture_notes.py --no-pdf         # md 만
  python lecture_notes.py --font "C:/Windows/Fonts/malgun.ttf"

산출물 (영상마다)
  output/노트/<영상>.md     문단 앞에 [시:분:초], 그 시각의 차트 프레임 링크
  output/노트/<영상>.pdf    같은 내용에 프레임 이미지를 넣은 PDF (Claude 프로젝트 / NotebookLM 에 올리는 용도)
  output/노트/색인.md       전체 목록

lecture_extract.py 의 테스트 산출물(이름이 _앞5분 으로 끝나는 것)은 건너뛴다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

TEST_SUFFIX = re.compile(r"_앞\d+(?:\.\d+)?분$")
FRAME_NAME = re.compile(r"^(\d+)m(\d+)s(?:_\d+)?\.jpe?g$", re.IGNORECASE)
FONT_CANDIDATES = [
    "C:/Windows/Fonts/malgun.ttf",                          # 맑은 고딕 (Windows)
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",      # 나눔고딕 (Linux: apt install fonts-nanum)
    "/Library/Fonts/NanumGothic.ttf",
    "~/Library/Fonts/NanumGothic.ttf",
]


def hms(sec: float) -> str:
    s = int(sec)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def parse_ts(ts: str) -> float:
    h, m, s = ts.strip().replace(",", ".").split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def parse_srt(path: Path) -> list[tuple[float, float, str]]:
    segs = []
    for block in path.read_text(encoding="utf-8").split("\n\n"):
        lines = [ln for ln in block.strip().splitlines() if ln.strip()]
        if len(lines) < 3 or "-->" not in lines[1]:
            continue
        a, b = lines[1].split("-->")
        segs.append((parse_ts(a), parse_ts(b), " ".join(lines[2:]).strip()))
    return segs


def load_frames(folder: Path) -> list[tuple[float, Path]]:
    out = []
    for p in folder.iterdir():
        m = FRAME_NAME.match(p.name)
        if m:
            out.append((int(m[1]) * 60 + int(m[2]), p))
    return sorted(out)


def build_timeline(segs, frames, para_sec: float, para_chars: int) -> list[tuple]:
    """('img', 초, 경로) 와 ('para', 시작초, 본문) 을 시간순으로 낸다."""
    events: list[tuple] = []
    para: list[tuple[float, float, str]] = []

    def flush():
        if para:
            events.append(("para", para[0][0], " ".join(t for _, _, t in para)))
            para.clear()

    fi = 0
    for seg in segs:
        while fi < len(frames) and frames[fi][0] <= seg[0]:
            flush()
            events.append(("img", *frames[fi]))
            fi += 1
        if para and (seg[0] - para[0][0] >= para_sec or sum(len(t) for _, _, t in para) >= para_chars):
            flush()
        para.append(seg)
    flush()
    for f in frames[fi:]:
        events.append(("img", *f))
    return events


def write_md(path: Path, title: str, meta: list[str], events, notes_dir: Path) -> None:
    lines = [f"# {title}", ""] + [f"- {m}" for m in meta] + [""]
    for ev in events:
        if ev[0] == "img":
            rel = Path(ev[2]).resolve().relative_to(notes_dir.parent.resolve()) if _inside(ev[2], notes_dir.parent) else ev[2]
            lines += [f"![{hms(ev[1])}](../{str(rel).replace(chr(92), '/')})", ""]
        else:
            lines += [f"**[{hms(ev[1])}]** {ev[2]}", ""]
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _inside(p: Path, root: Path) -> bool:
    try:
        Path(p).resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def find_font(explicit: str | None) -> Path | None:
    for c in ([explicit] if explicit else []) + FONT_CANDIDATES:
        p = Path(c).expanduser()
        if p.is_file():
            return p
    return None


def write_pdf(path: Path, title: str, meta: list[str], events, font: Path, img_w: float = 180) -> int:
    import logging
    from fpdf import FPDF

    logging.getLogger("fontTools").setLevel(logging.ERROR)  # 폰트 서브셋 때 나오는 "TSI0 NOT subset" 잡음 숨김

    pdf = FPDF(format="A4")
    pdf.set_margins(15, 15, 15)
    pdf.set_auto_page_break(True, margin=15)
    pdf.add_font("kr", "", str(font))
    bold = font.with_name(font.stem + "bd" + font.suffix)  # malgun → malgunbd
    pdf.add_font("kr", "B", str(bold if bold.is_file() else font))
    pdf.add_page()
    pdf.set_font("kr", "B", 16)
    pdf.multi_cell(0, 9, title, new_x="LMARGIN", new_y="NEXT", wrapmode="CHAR")
    pdf.set_font("kr", "", 9)
    pdf.set_text_color(110, 110, 110)
    for m in meta:
        pdf.multi_cell(0, 5, m, new_x="LMARGIN", new_y="NEXT", wrapmode="CHAR")
    pdf.ln(3)
    for ev in events:
        if ev[0] == "img":
            img_h = img_w * 9 / 16  # 1280×720 기준. 다른 비율이면 fpdf 가 실제 비율로 그린다(높이는 w 로만 지정)
            if pdf.get_y() + img_h + 8 > pdf.page_break_trigger:
                pdf.add_page()
            pdf.set_font("kr", "", 8)
            pdf.set_text_color(110, 110, 110)
            pdf.cell(0, 5, f"[{hms(ev[1])}] 화면", new_x="LMARGIN", new_y="NEXT")
            pdf.image(str(ev[2]), w=img_w)
            pdf.ln(3)
        else:
            pdf.set_font("kr", "B", 9)
            pdf.set_text_color(70, 70, 70)
            pdf.cell(0, 5, f"[{hms(ev[1])}]", new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("kr", "", 10.5)
            pdf.set_text_color(0, 0, 0)
            pdf.multi_cell(0, 6, ev[2], new_x="LMARGIN", new_y="NEXT", wrapmode="CHAR")
            pdf.ln(2)
    pdf.output(str(path))
    return pdf.pages_count


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="output", help="lecture_extract.py 의 산출물 폴더 (기본 ./output)")
    ap.add_argument("--no-pdf", action="store_true", help="PDF 생략, md 만")
    ap.add_argument("--font", help="PDF 용 한글 TTF 경로 (기본: 맑은 고딕 → 나눔고딕 순으로 찾음)")
    ap.add_argument("--para-sec", type=float, default=60, help="문단 하나의 최대 길이(초, 기본 60)")
    ap.add_argument("--para-chars", type=int, default=400, help="문단 하나의 최대 글자 수(기본 400)")
    ap.add_argument("--force", action="store_true", help="이미 있는 노트도 다시 만든다")
    args = ap.parse_args(argv)

    out = Path(args.out)
    sub_dir, frame_dir, notes_dir = out / "자막", out / "프레임", out / "노트"
    if not sub_dir.is_dir() and not frame_dir.is_dir():
        sys.exit(f"오류: {sub_dir} 도 {frame_dir} 도 없습니다. lecture_extract.py run 을 먼저 돌리세요.")
    notes_dir.mkdir(parents=True, exist_ok=True)

    stems = {p.stem for p in sub_dir.glob("*.srt")} if sub_dir.is_dir() else set()
    stems |= {p.name for p in frame_dir.iterdir() if p.is_dir()} if frame_dir.is_dir() else set()
    stems = sorted((s for s in stems if not TEST_SUFFIX.search(s)), key=lambda s: [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)])
    if not stems:
        sys.exit("오류: 노트로 만들 자막/프레임이 없습니다.")

    font = None
    if not args.no_pdf:
        try:
            import fpdf  # noqa: F401
        except ImportError:
            sys.exit("오류: PDF 를 만들려면  pip install fpdf2  가 필요합니다. (md 만 원하면 --no-pdf)")
        font = find_font(args.font)
        if not font:
            sys.exit("오류: 한글 폰트를 찾지 못했습니다. --font 로 TTF 경로를 주세요 (예: C:/Windows/Fonts/malgun.ttf)")

    rows = []
    for stem in stems:
        srt = sub_dir / f"{stem}.srt"
        fdir = frame_dir / stem
        segs = parse_srt(srt) if srt.is_file() else []
        frames = load_frames(fdir) if fdir.is_dir() else []
        md_path, pdf_path = notes_dir / f"{stem}.md", notes_dir / f"{stem}.pdf"
        if not args.force and md_path.exists() and (args.no_pdf or pdf_path.exists()):
            print(f"[{stem}] 이미 있음 → 건너뜀 (--force 로 다시)")
            rows.append((stem, len(segs), len(frames), "있음"))
            continue
        dur = max([e for _, e, _ in segs] + [t for t, _ in frames] + [0])
        meta = [f"길이 {hms(dur)} · 자막 {len(segs)}구간 · 화면 {len(frames)}장",
                f"자막: {srt.name if srt.is_file() else '없음'} · 프레임 폴더: {fdir.name if fdir.is_dir() else '없음'}",
                f"생성 {dt.datetime.now():%Y-%m-%d %H:%M}"]
        events = build_timeline(segs, frames, args.para_sec, args.para_chars)
        write_md(md_path, stem, meta, events, notes_dir)
        pages = "-"
        if not args.no_pdf:
            pages = write_pdf(pdf_path, stem, meta, events, font)
        print(f"[{stem}] 자막 {len(segs)}구간 + 화면 {len(frames)}장 → md" + (f", pdf {pages}쪽" if pages != "-" else ""))
        rows.append((stem, len(segs), len(frames), f"{pages}쪽" if pages != "-" else "md"))

    idx = [f"# 강의 노트 색인  ({dt.datetime.now():%Y-%m-%d %H:%M})", "",
           "| # | 영상 | 자막 구간 | 화면 | PDF |", "|---|---|---|---|---|"]
    idx += [f"| {i} | [{s}]({s}.md) | {n} | {f} | {p} |" for i, (s, n, f, p) in enumerate(rows, 1)]
    (notes_dir / "색인.md").write_text("\n".join(idx) + "\n", encoding="utf-8")
    print(f"\n완료: {len(rows)}편 → {notes_dir}  (색인: {notes_dir / '색인.md'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
