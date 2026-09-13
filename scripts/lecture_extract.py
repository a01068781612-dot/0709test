#!/usr/bin/env python3
"""강의 영상 → 한국어 자막(srt) + 장면 전환 프레임(jpg).

  python scripts/lecture_extract.py check          # ffmpeg / whisper 설치 확인
  python scripts/lecture_extract.py list           # 영상 목록과 길이 (하위 폴더 포함)
  python scripts/lecture_extract.py run --test     # 첫 영상 앞 5분만 시험
  python scripts/lecture_extract.py run            # 전체 영상

폴더를 생략하면 ~/OneDrive/바탕 화면/네이버카페영상 → ~/Desktop/네이버카페영상 순으로 찾는다.
다른 곳이면 첫 인자로 적는다:  python scripts/lecture_extract.py list "D:\\강의\\네이버카페영상"

산출물 (기본 --out output)
  output/자막/<영상>.srt, <영상>.txt
  output/프레임/<영상>/0014m32s.jpg     ← 분·초 타임스탬프, 가로 1280px
  output/프레임/<영상>/_중복제거.txt     ← 중복으로 버린 프레임 목록 (중복 제거가 돌았을 때만)
  output/요약.md

필요한 것: ffmpeg, faster-whisper (없으면 openai-whisper). 둘 다 없으면 `check` 가 설치 명령을 보여준다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Windows + Anaconda: MKL(numpy)과 ctranslate2 가 각자 OpenMP(libiomp5md.dll)를 실어
# "OMP: Error #15" 로 죽는다. whisper 를 불러오기 전에 중복 로드를 허용해 둔다.
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")  # 모델 캐시 심볼릭 링크 경고 숨김

VIDEO_EXT = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".wmv", ".m4v", ".ts", ".mts", ".flv", ".mpg", ".mpeg"}
DEFAULT_INPUTS = ["~/OneDrive/바탕 화면/네이버카페영상", "~/Desktop/네이버카페영상", "~/OneDrive/Desktop/네이버카페영상"]
DEFAULT_PROMPT = "주식 차트 강의입니다. 이동평균선, 거래량, 지지선, 저항선, 매수, 매도, 눌림목, 돌파."
HASH_W, HASH_H = 17, 16  # dHash 축소 크기 → 16×16 = 256비트 해시

INSTALL_FFMPEG = """\
  ffmpeg 설치
    Windows : winget install Gyan.FFmpeg        (또는 choco install ffmpeg) → 설치 후 터미널 새로 열기
    macOS   : brew install ffmpeg
    Ubuntu  : sudo apt install ffmpeg
    대안    : pip install imageio-ffmpeg           (파이썬 패키지에 든 ffmpeg 를 스크립트가 자동으로 찾음)"""
INSTALL_WHISPER = """\
  faster-whisper 설치 (권장, CPU 에서도 빠름)
    pip install faster-whisper
    NVIDIA GPU 가 있으면 추가로:  pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
  대안: openai-whisper
    pip install openai-whisper
  ※ 모델(medium ≈ 1.5GB)은 첫 실행 때 자동으로 내려받는다 (인터넷 필요)."""


# ────────────────────────── 공통 ──────────────────────────
def die(msg: str) -> None:
    sys.exit(f"오류: {msg}")


def natural_key(rel: Path):
    """'1강' < '2강' < '10강' 이 되도록 숫자는 수로 비교한다. 하위 폴더도 같은 규칙."""
    return [[int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", part)] for part in rel.parts]


def find_videos(folder: Path) -> list[Path]:
    """하위 폴더(예: 1,2강/ 3,4,5강/)까지 훑어 영상 파일을 자연 정렬로 돌려준다."""
    vids = [p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_EXT and not p.name.startswith(".")]
    return sorted(vids, key=lambda p: natural_key(p.relative_to(folder)))


def output_stems(videos: list[Path]) -> dict[Path, str]:
    """산출물 이름은 파일명. 다른 폴더에 같은 이름이 있으면 폴더명을 앞에 붙인다."""
    counts: dict[str, int] = {}
    for v in videos:
        counts[v.stem] = counts.get(v.stem, 0) + 1
    return {v: v.stem if counts[v.stem] == 1 else f"{v.parent.name}_{v.stem}" for v in videos}


def hms(sec: float | None) -> str:
    if sec is None:
        return "?"
    s = int(round(sec))
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def srt_ts(sec: float) -> str:
    ms = max(0, int(round(sec * 1000)))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def frame_name(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    return f"{m:04d}m{s:02d}s"


# ────────────────────────── ffmpeg ──────────────────────────
def find_ffmpeg(explicit: str | None = None) -> str | None:
    for c in (explicit, os.environ.get("FFMPEG"), shutil.which("ffmpeg")):
        if c and Path(c).exists():
            return str(c)
    try:
        import imageio_ffmpeg  # type: ignore
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def ffmpeg_version(ffmpeg: str) -> tuple[int, int]:
    out = subprocess.run([ffmpeg, "-version"], capture_output=True, text=True, errors="replace").stdout
    m = re.search(r"ffmpeg version\s+\S*?(\d+)\.(\d+)", out)
    return (int(m.group(1)), int(m.group(2))) if m else (99, 0)


def vfr_args(ffmpeg: str) -> list[str]:
    # select 로 골라낸 프레임을 그대로 쓰려면 가변 프레임레이트가 필요하다. 5.1 부터 -fps_mode, 그 전엔 -vsync.
    return ["-fps_mode", "vfr"] if ffmpeg_version(ffmpeg) >= (5, 1) else ["-vsync", "vfr"]


def probe(ffmpeg: str, path: Path) -> dict:
    p = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)], capture_output=True, text=True, errors="replace")
    err = p.stderr
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", err)
    duration = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else None
    vline = next((ln for ln in err.splitlines() if "Video:" in ln), "")
    res = re.search(r"[ ,](\d{2,5})x(\d{2,5})", vline)
    fps = re.search(r"([\d.]+) fps", vline)
    return {
        "duration": duration,
        "size": f"{res[1]}x{res[2]}" if res else "?",
        "fps": fps[1] if fps else "?",
        "has_video": bool(vline),
        "has_audio": "Audio:" in err,
    }


def run_ffmpeg(cmd: list[str], what: str) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if p.returncode:
        die(f"{what} 실패 (ffmpeg 종료코드 {p.returncode})\n{p.stderr[-2000:]}")
    return p


def extract_audio(ffmpeg: str, video: Path, wav: Path, limit: float | None) -> None:
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y"]
    if limit:
        cmd += ["-t", f"{limit:.3f}"]
    cmd += ["-i", str(video), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)]
    run_ffmpeg(cmd, "음성 추출")


def extract_frames(ffmpeg: str, video: Path, work: Path, limit: float | None, threshold: float, width: int,
                   interval: float = 0) -> tuple[list[float], list[int]]:
    """장면이 바뀐 프레임(+ interval 초마다 한 장)을 한 번의 디코딩으로 뽑는다.

    같은 프레임을 두 갈래로 내보낸다: (a) 가로 `width`px JPG, (b) 17×16 회색 축소본(중복 판정용 dHash 재료).
    돌려주는 값: (프레임별 시각(초), 프레임별 dHash) — 순서는 저장된 f_000001.jpg … 와 같다.
    """
    expr = f"eq(n,0)+gt(scene,{threshold})"
    if interval > 0:
        expr += f"+gte(t-prev_selected_t,{interval})"  # 마지막으로 뽑은 뒤 interval 초가 지났으면 한 장 더
    fc = (
        f"[0:v]select='{expr}',showinfo,split=2[a][b];"
        f"[a]scale={width}:-2[big];"
        f"[b]scale={HASH_W}:{HASH_H}:flags=area,format=gray[small]"
    )
    vfr = vfr_args(ffmpeg)
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-loglevel", "info", "-y"]
    if limit:
        cmd += ["-t", f"{limit:.3f}"]
    cmd += ["-i", str(video), "-filter_complex", fc,
            "-map", "[big]", *vfr, "-q:v", "2", "-f", "image2", str(work / "f_%06d.jpg"),
            "-map", "[small]", *vfr, "-f", "rawvideo", "-pix_fmt", "gray", str(work / "hash.raw")]
    p = run_ffmpeg(cmd, "프레임 추출")
    times = [float(t) for t in re.findall(r"pts_time:\s*(-?[0-9.]+)", p.stderr)]
    hashes = dhash_all((work / "hash.raw").read_bytes()) if (work / "hash.raw").exists() else []
    jpgs = sorted(work.glob("f_*.jpg"))
    if not (len(times) == len(hashes) == len(jpgs)):
        die(f"프레임 수가 맞지 않습니다: 시각 {len(times)} / 해시 {len(hashes)} / jpg {len(jpgs)} — 작업 폴더 {work}")
    return times, hashes


# ────────────────────────── 중복 제거 ──────────────────────────
def dhash_all(raw: bytes) -> list[int]:
    n = HASH_W * HASH_H
    out = []
    for i in range(0, len(raw) - n + 1, n):
        blk = raw[i:i + n]
        h = 0
        for r in range(HASH_H):
            row = blk[r * HASH_W:(r + 1) * HASH_W]
            for c in range(HASH_W - 1):
                h = (h << 1) | (1 if row[c] > row[c + 1] else 0)
        out.append(h)
    return out


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def dedup(hashes: list[int], threshold: int) -> tuple[list[int], dict[int, tuple[int, int]]]:
    """앞선 프레임과 해시 거리가 threshold 이하이면 중복으로 본다. (남길 인덱스, {버린 인덱스: (원본 인덱스, 거리)})"""
    kept: list[int] = []
    dropped: dict[int, tuple[int, int]] = {}
    for i, h in enumerate(hashes):
        best = None
        for k in kept:
            d = hamming(h, hashes[k])
            if d <= threshold and (best is None or d < best[1]):
                best = (k, d)
        if best is None:
            kept.append(i)
        else:
            dropped[i] = best
    return kept, dropped


# ────────────────────────── STT ──────────────────────────
class Stt:
    def __init__(self, engine: str, model: str, device: str, compute_type: str, prompt: str, beam: int):
        self.engine = self._resolve_engine(engine)
        self.model_name, self.device, self.prompt, self.beam = model, device, prompt, beam
        self.compute_type = self._pick_compute(device, compute_type) if self.engine == "faster" else "-"
        self.model = None

    @staticmethod
    def _resolve_engine(engine: str) -> str:
        if engine in ("auto", "faster"):
            try:
                import faster_whisper  # noqa: F401
                return "faster"
            except ImportError:
                if engine == "faster":
                    die("faster-whisper 가 없습니다.\n" + INSTALL_WHISPER)
        if engine in ("auto", "openai"):
            try:
                import whisper  # noqa: F401
                return "openai"
            except ImportError:
                if engine == "openai":
                    die("openai-whisper 가 없습니다.\n" + INSTALL_WHISPER)
        die("STT 엔진이 없습니다.\n" + INSTALL_WHISPER)
        return ""

    @staticmethod
    def _pick_compute(device: str, compute_type: str) -> str:
        if compute_type != "auto":
            return compute_type
        if device == "cuda":
            return "float16"
        if device == "cpu":
            return "int8"
        try:
            import ctranslate2  # type: ignore
            return "float16" if ctranslate2.get_cuda_device_count() > 0 else "int8"
        except Exception:
            return "int8"

    def describe(self) -> str:
        return f"{self.engine}-whisper {self.model_name} ({self.device}/{self.compute_type})"

    def load(self) -> None:
        t = time.time()
        if self.engine == "faster":
            from faster_whisper import WhisperModel
            self.model = WhisperModel(self.model_name, device=self.device, compute_type=self.compute_type)
        else:
            import whisper
            self.model = whisper.load_model(self.model_name, device=None if self.device == "auto" else self.device)
        print(f"  모델 준비: {self.describe()}  ({time.time() - t:.0f}초)")

    def run(self, wav: Path):
        """(시작초, 끝초, 문장) 을 순서대로 낸다."""
        if self.engine == "faster":
            segs, _info = self.model.transcribe(str(wav), language="ko", beam_size=self.beam,
                                                vad_filter=True, initial_prompt=self.prompt or None)
            for s in segs:
                yield s.start, s.end, s.text.strip()
        else:
            res = self.model.transcribe(str(wav), language="ko", beam_size=self.beam,
                                        initial_prompt=self.prompt or None, verbose=False)
            for s in res["segments"]:
                yield s["start"], s["end"], s["text"].strip()


def write_srt(segs: list[tuple[float, float, str]], srt: Path, txt: Path) -> None:
    lines = []
    for i, (st, en, tx) in enumerate(segs, 1):
        en = max(en, st + 0.1)
        lines.append(f"{i}\n{srt_ts(st)} --> {srt_ts(en)}\n{tx}\n")
    srt.write_text("\n".join(lines), encoding="utf-8")
    txt.write_text("\n".join(f"[{hms(st)}] {tx}" for st, _, tx in segs) + "\n", encoding="utf-8")


# ────────────────────────── 명령 ──────────────────────────
def resolve_input(arg: str | None) -> Path:
    if arg:
        folder = Path(arg).expanduser()
        if not folder.is_dir():
            die(f"폴더가 없습니다: {folder}")
        return folder
    for cand in DEFAULT_INPUTS:
        folder = Path(cand).expanduser()
        if folder.is_dir():
            return folder
    die("영상 폴더를 찾지 못했습니다. 경로를 직접 적어 주세요.\n"
        "  예) python scripts/lecture_extract.py list \"C:\\Users\\spf38\\OneDrive\\바탕 화면\\네이버카페영상\"")
    return Path()


def cmd_check(args) -> int:
    ok = True
    ffmpeg = find_ffmpeg(args.ffmpeg)
    if ffmpeg:
        print(f"[OK]   ffmpeg {'.'.join(map(str, ffmpeg_version(ffmpeg)))}  →  {ffmpeg}")
    else:
        ok = False
        print("[없음] ffmpeg\n" + INSTALL_FFMPEG)
    found = None
    for mod, label in (("faster_whisper", "faster-whisper"), ("whisper", "openai-whisper")):
        try:
            m = __import__(mod)
            ver = getattr(m, "__version__", "")
            print(f"[OK]   {label} {ver}")
            found = found or label
        except ImportError:
            print(f"[없음] {label}")
    if not found:
        ok = False
        print(INSTALL_WHISPER)
    try:
        import ctranslate2  # type: ignore
        n = ctranslate2.get_cuda_device_count()
        print(f"[정보] GPU(CUDA) {n}개 감지" if n else "[정보] GPU 없음 → CPU(int8) 로 돌립니다. medium 모델은 실시간의 1~2배 정도 걸립니다.")
    except Exception:
        pass
    print("\n준비 완료. 다음: list → run --test" if ok else "\n위 항목을 설치한 뒤 다시 check 를 실행하세요.")
    return 0 if ok else 1


def cmd_list(args) -> int:
    folder = resolve_input(args.input)
    ffmpeg = find_ffmpeg(args.ffmpeg) or die("ffmpeg 가 없습니다.\n" + INSTALL_FFMPEG)
    videos = find_videos(folder)
    if not videos:
        die(f"영상 파일이 없습니다: {folder}")
    print(f"폴더: {folder}\n")
    print(f"{'#':>2}  {'길이':>8}  {'해상도':>9}  {'fps':>5}  {'크기':>8}  파일")
    total = 0.0
    for i, v in enumerate(videos, 1):
        info = probe(ffmpeg, v)
        total += info["duration"] or 0
        mb = v.stat().st_size / 1e6
        print(f"{i:>2}  {hms(info['duration']):>8}  {info['size']:>9}  {info['fps']:>5}  {mb:7.0f}M  {v.relative_to(folder)}")
    print(f"\n합계 {len(videos)}편, {hms(total)}")
    return 0


def select_videos(videos: list[Path], select: str | None) -> list[Path]:
    if not select:
        return videos
    chosen = []
    for tok in select.split(","):
        tok = tok.strip()
        if tok.isdigit() and 1 <= int(tok) <= len(videos):
            chosen.append(videos[int(tok) - 1])
        else:
            hit = [v for v in videos if tok in str(v)]
            if not hit:
                die(f"--select '{tok}' 에 해당하는 영상이 없습니다 (list 로 번호를 확인하세요)")
            chosen += hit
    return chosen


def cmd_run(args) -> int:
    folder = resolve_input(args.input)
    ffmpeg = find_ffmpeg(args.ffmpeg) or die("ffmpeg 가 없습니다.\n" + INSTALL_FFMPEG)
    videos = select_videos(find_videos(folder), args.select)
    if not videos:
        die(f"영상 파일이 없습니다: {folder}")
    limit = None
    if args.test:
        videos, limit = videos[:1], args.test_minutes * 60.0
    stems = output_stems(videos)
    out = Path(args.out)
    sub_dir, frame_dir, work_root = out / "자막", out / "프레임", out / "_work"
    sub_dir.mkdir(parents=True, exist_ok=True)
    frame_dir.mkdir(parents=True, exist_ok=True)

    stt = None
    if not args.skip_stt:
        stt = Stt(args.engine, args.model, args.device, args.compute_type, args.prompt, args.beam)
        stt.load()

    mode = f"테스트 (첫 영상 앞 {args.test_minutes:g}분)" if args.test else f"전체 {len(videos)}편"
    print(f"\n입력: {folder}\n모드: {mode}\nSTT : {stt.describe() if stt else '건너뜀'}\n")
    rows = []
    for idx, video in enumerate(videos, 1):
        t0 = time.time()
        info = probe(ffmpeg, video)
        if not info["has_video"]:
            print(f"[{idx}/{len(videos)}] {video.name}: 영상 트랙이 없어 건너뜀")
            continue
        dur = info["duration"] or 0
        if limit:
            dur = min(dur, limit)
        stem = stems[video] + (f"_앞{args.test_minutes:g}분" if limit else "")
        print(f"[{idx}/{len(videos)}] {video.relative_to(folder)}  ({hms(dur)}, {info['size']})")
        work = work_root / stem
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        row = {"영상": str(video.relative_to(folder)), "길이": hms(dur), "자막": "-", "구간": 0, "프레임": "-", "감지": 0, "저장": 0}

        # 1) 음성 → 자막
        if stt and info["has_audio"]:
            wav = work / "audio.wav"
            extract_audio(ffmpeg, video, wav, limit)
            segs: list[tuple[float, float, str]] = []
            last = 0.0
            for st, en, tx in stt.run(wav):
                if tx:
                    segs.append((st, en, tx))
                if dur and time.time() - last > 3:
                    print(f"\r  STT {min(en / dur, 1):5.1%}  ({len(segs)} 구간)", end="", flush=True)
                    last = time.time()
            print(f"\r  STT 완료: {len(segs)} 구간" + " " * 20)
            srt = sub_dir / f"{stem}.srt"
            write_srt(segs, srt, sub_dir / f"{stem}.txt")
            row["자막"], row["구간"] = str(srt), len(segs)
        elif stt:
            print("  오디오 트랙이 없어 자막은 건너뜀")

        # 2) 장면 전환 프레임
        if not args.skip_frames:
            times, hashes = extract_frames(ffmpeg, video, work, limit, args.scene_threshold, args.width, args.interval)
            n_raw = len(times)
            kept, dropped = list(range(n_raw)), {}
            if args.dedup == "always" or (args.dedup == "auto" and n_raw > args.max_frames):
                kept, dropped = dedup(hashes, args.dedup_distance)
            dest = frame_dir / stem
            shutil.rmtree(dest, ignore_errors=True)
            dest.mkdir(parents=True)
            used: dict[str, int] = {}
            names: dict[int, str] = {}
            for i in kept:
                base = frame_name(times[i])
                used[base] = used.get(base, 0) + 1
                name = f"{base}.jpg" if used[base] == 1 else f"{base}_{used[base]}.jpg"
                shutil.move(str(work / f"f_{i + 1:06d}.jpg"), str(dest / name))
                names[i] = name
            if dropped:
                report = [f"장면 전환 {n_raw}장 중 {len(dropped)}장을 중복으로 제거 (해시 거리 ≤ {args.dedup_distance})", ""]
                report += [f"[제거] {frame_name(times[i])}  ≈ {names[k]} (거리 {d})" for i, (k, d) in sorted(dropped.items())]
                (dest / "_중복제거.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
            print(f"  프레임: 장면 전환 {n_raw}장 → 저장 {len(kept)}장  ({dest})")
            row["프레임"], row["감지"], row["저장"] = str(dest), n_raw, len(kept)

        if not args.keep_work:
            shutil.rmtree(work, ignore_errors=True)
        row["소요"] = f"{time.time() - t0:.0f}초"
        rows.append(row)

    if not args.keep_work:
        shutil.rmtree(work_root, ignore_errors=True)

    # 3) 요약
    lines = [f"# 추출 결과  ({dt.datetime.now():%Y-%m-%d %H:%M})", "",
             f"- 입력: `{folder}`", f"- 모드: {mode}", f"- STT: {stt.describe() if stt else '건너뜀'}",
             f"- 장면 전환 기준 {args.scene_threshold}, 추가 샘플 {f'{args.interval:g}초마다' if args.interval else '없음'}, 프레임 폭 {args.width}px, "
             f"중복 제거 {args.dedup} (기준 {args.max_frames}장, 거리 ≤ {args.dedup_distance})", "",
             "| # | 영상 | 길이 | 자막 구간 | 프레임 감지→저장 | 소요 |", "|---|---|---|---|---|---|"]
    lines += [f"| {i} | {r['영상']} | {r['길이']} | {r['구간'] if r['자막'] != '-' else '-'} | {r['감지']}→{r['저장']} | {r['소요']} |"
              for i, r in enumerate(rows, 1)]
    lines += ["", "## 산출물", ""]
    for r in rows:
        if r["자막"] != "-":
            lines.append(f"- {r['자막']}")
        if r["프레임"] != "-":
            lines.append(f"- {r['프레임']}/")
    (out / "요약.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n" + "\n".join(lines[6:]))
    print(f"\n요약 파일: {out / '요약.md'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # 윈도우 cp949 콘솔 대비
    except Exception:
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ffmpeg", help="ffmpeg 실행 파일 경로 (기본: PATH → imageio-ffmpeg)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check", help="도구 설치 확인")

    p_list = sub.add_parser("list", help="영상 목록과 길이")
    p_list.add_argument("input", nargs="?", help=f"영상 폴더, 하위 폴더 포함 (기본 {DEFAULT_INPUTS[0]})")

    p_run = sub.add_parser("run", help="자막 + 프레임 추출")
    p_run.add_argument("input", nargs="?", help=f"영상 폴더, 하위 폴더 포함 (기본 {DEFAULT_INPUTS[0]})")
    p_run.add_argument("--out", default="output", help="산출물 폴더 (기본 ./output)")
    p_run.add_argument("--test", action="store_true", help="첫 번째 영상의 앞부분만 (--test-minutes)")
    p_run.add_argument("--test-minutes", type=float, default=5, help="--test 때 자를 길이 (기본 5분)")
    p_run.add_argument("--select", help="특정 영상만: list 번호나 파일명 일부, 쉼표 구분 (예: 1,3 또는 3강)")
    p_run.add_argument("--skip-stt", action="store_true", help="자막 생략")
    p_run.add_argument("--skip-frames", action="store_true", help="프레임 생략")
    g = p_run.add_argument_group("자막(STT)")
    g.add_argument("--engine", choices=["auto", "faster", "openai"], default="auto")
    g.add_argument("--model", default="medium", help="whisper 모델: tiny/base/small/medium/large-v3 (기본 medium)")
    g.add_argument("--device", default="auto", help="auto/cpu/cuda")
    g.add_argument("--compute-type", default="auto", help="faster-whisper 연산 형식: auto/int8/float16/float32")
    g.add_argument("--beam", type=int, default=5)
    g.add_argument("--prompt", default=DEFAULT_PROMPT, help="용어 힌트 (빈 문자열이면 사용 안 함)")
    g = p_run.add_argument_group("프레임")
    g.add_argument("--scene-threshold", type=float, default=0.3, help="장면 전환 민감도 0~1, 낮을수록 많이 뽑음 (기본 0.3)")
    g.add_argument("--interval", type=float, default=0, help="이 초마다 한 장씩 추가로 뽑음, 0이면 장면 전환만 (기본 0)")
    g.add_argument("--width", type=int, default=1280, help="저장 프레임 가로 px (기본 1280)")
    g.add_argument("--max-frames", type=int, default=300, help="이 장수를 넘으면 중복 제거 (기본 300)")
    g.add_argument("--dedup", choices=["auto", "always", "never"], default="auto")
    g.add_argument("--dedup-distance", type=int, default=10, help="중복으로 볼 dHash 거리 (256비트 중, 기본 10)")
    p_run.add_argument("--keep-work", action="store_true", help="wav 등 중간 파일을 output/_work 에 남김")

    args = ap.parse_args(argv)
    return {"check": cmd_check, "list": cmd_list, "run": cmd_run}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
