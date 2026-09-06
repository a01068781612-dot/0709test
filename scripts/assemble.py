#!/usr/bin/env python3
"""나레이션 wav 를 영상에 얹는다.

제미나이 낭독이 대본 타임코드보다 길어서, 컷이 밀리지 않게 화면을 늘린다.
각 컷의 마지막 프레임을 모자란 만큼 정지시켜(freeze) 이어 붙이는 방식이라
원본 화면은 그대로 두고 길이만 맞춘다.

  python3 scripts/assemble.py --video <mp4> --wav wav --out out/최종.mp4

산출물: 최종 mp4 + 늘어난 시각에 맞춘 srt/vtt
"""
import argparse, json, os, subprocess, sys

FFMPEG = "/usr/local/lib/python3.11/dist-packages/imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v7.0.2"


def run(args: list[str]) -> None:
    p = subprocess.run(args, capture_output=True, text=True)
    if p.returncode:
        sys.exit(f"ffmpeg 실패:\n{' '.join(args[:6])}...\n{p.stderr[-1500:]}")


def probe_dur(path: str) -> float:
    p = subprocess.run([FFMPEG, "-hide_banner", "-i", path], capture_output=True, text=True)
    for line in p.stderr.splitlines():
        if "Duration:" in line:
            h, m, s = line.split("Duration:")[1].split(",")[0].strip().split(":")
            return int(h) * 3600 + int(m) * 60 + float(s)
    sys.exit(f"길이를 읽지 못했습니다: {path}")


def ts(sec: float, comma: bool = True) -> str:
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}".replace(".", "," if comma else ".")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--wav", default="wav")
    ap.add_argument("--cues", default="docs/cues.json")
    ap.add_argument("--out", default="out/최종.mp4")
    ap.add_argument("--gap", type=float, default=0.35, help="컷 사이 최소 숨 (초)")
    ap.add_argument("--work", default="out/_seg")
    args = ap.parse_args()

    cues = json.load(open(args.cues, encoding="utf-8"))
    lines = cues["lines"]
    video_dur = probe_dur(args.video)
    os.makedirs(args.work, exist_ok=True)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)

    # 1) 컷별로 필요한 시간을 정한다 — 원래 슬롯과 실제 낭독 길이 중 큰 쪽.
    plan = []
    for i, l in enumerate(lines):
        wav = os.path.join(args.wav, f"{l['id']:02d}.wav")
        if not os.path.exists(wav):
            sys.exit(f"음성 파일이 없습니다: {wav}")
        audio = probe_dur(wav)
        # 이 컷이 차지하는 원본 화면 구간 — 다음 컷 시작까지, 마지막은 영상 끝까지.
        src_start = l["start"] if i else 0.0
        src_end = lines[i + 1]["start"] if i + 1 < len(lines) else video_dur
        src_len = max(src_end - src_start, 0.05)
        need = audio + args.gap
        plan.append({"id": l["id"], "text": l["text"], "wav": wav, "audio": audio,
                     "src_start": src_start, "src_len": src_len,
                     "pad": max(need - src_len, 0.0), "out_len": max(src_len, need)})

    total = sum(p["out_len"] for p in plan)
    print(f"원본 {video_dur:.1f}초 → 결과 {total:.1f}초 (정지 프레임 {total - video_dur:+.1f}초)\n")

    # 2) 구간별로 잘라내고 모자란 만큼 마지막 프레임을 정지시킨다.
    seg_list, a_list = [], []
    for p in plan:
        vseg = os.path.join(args.work, f"v{p['id']:02d}.mp4")
        vf = f"tpad=stop_mode=clone:stop_duration={p['pad']:.3f}" if p["pad"] > 0.001 else "null"
        run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
             "-ss", f"{p['src_start']:.3f}", "-t", f"{p['src_len']:.3f}", "-i", args.video,
             "-an", "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
             "-pix_fmt", "yuv420p", "-r", "30", vseg])
        seg_list.append(vseg)

        # 오디오도 같은 길이로 — 낭독 뒤에 무음을 채워 컷 경계를 맞춘다.
        aseg = os.path.join(args.work, f"a{p['id']:02d}.wav")
        run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", p["wav"],
             "-af", f"apad=whole_dur={p['out_len']:.3f},aresample=48000", "-ac", "2", aseg])
        a_list.append(aseg)
        print(f"  {p['id']:>2} 낭독 {p['audio']:>5.2f} · 화면 {p['src_len']:>5.2f} → {p['out_len']:>5.2f}초"
              + (f"  (+{p['pad']:.2f} 정지)" if p["pad"] > 0.001 else ""))

    def concat(files: list[str], out: str, extra: list[str]) -> None:
        lst = os.path.join(args.work, os.path.basename(out) + ".txt")
        with open(lst, "w", encoding="utf-8") as f:
            for x in files:
                f.write(f"file '{os.path.abspath(x)}'\n")
        run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
             "-f", "concat", "-safe", "0", "-i", lst, *extra, out])

    vcat = os.path.join(args.work, "video.mp4")
    acat = os.path.join(args.work, "audio.wav")
    concat(seg_list, vcat, ["-c", "copy"])
    concat(a_list, acat, ["-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2"])

    # 3) 화면과 소리를 합친다.
    run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", vcat, "-i", acat,
         "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", args.out])

    # 4) 늘어난 시각에 맞춰 자막을 다시 쓴다.
    t, srt, vtt = 0.0, [], ["WEBVTT", ""]
    for i, p in enumerate(plan, 1):
        s, e = t, t + p["audio"]
        srt.append(f"{i}\n{ts(s)} --> {ts(e)}\n{p['text']}\n")
        vtt += [f"{ts(s, False)} --> {ts(e, False)}", p["text"], ""]
        t += p["out_len"]
    base = os.path.splitext(args.out)[0]
    open(base + ".srt", "w", encoding="utf-8").write("\n".join(srt))
    open(base + ".vtt", "w", encoding="utf-8").write("\n".join(vtt))

    print(f"\n영상 {args.out}\n자막 {base}.srt · {base}.vtt\n최종 {probe_dur(args.out):.1f}초")


if __name__ == "__main__":
    main()
