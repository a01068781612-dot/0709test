#!/usr/bin/env python3
"""Gemini TTS 로 docs/cues.json 의 대사를 컷별 wav 로 만든다.

Gemini 는 오디오를 응답 본문에 base64 PCM 으로 실어 보낸다(CDN 경유 없음).
그래서 이 컨테이너 안에서 바로 파일로 떨어지고, 길이를 재서 슬롯 초과를 판정할 수 있다.

  GEMINI_API_KEY=... python3 scripts/tts_gemini.py
  GEMINI_API_KEY=... python3 scripts/tts_gemini.py --voices Charon,Iapetus,Alnilam --only 1,2,3

출력: wav/NN.wav  +  wav/report.json
"""
import argparse, base64, json, os, re, struct, sys, time, urllib.error, urllib.request

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"


class NoAudio(RuntimeError):
    """모델이 오디오 대신 텍스트를 냈다 — 짧은 문장에서 가끔 발생한다."""


def wav_from_pcm(pcm: bytes, rate: int) -> bytes:
    """Gemini 출력은 헤더 없는 16bit LE 모노 PCM 이라 WAV 헤더를 직접 붙인다."""
    return b"".join([
        b"RIFF", struct.pack("<I", 36 + len(pcm)), b"WAVE",
        b"fmt ", struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16),
        b"data", struct.pack("<I", len(pcm)), pcm,
    ])


def synth(text: str, voice: str, model: str, key: str, tone: str, retries: int = 3) -> tuple[bytes, int]:
    body = json.dumps({
        "contents": [{"role": "user", "parts": [{"text": f"{tone}:\n\n{text}"}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}},
        },
    }).encode()
    url = API.format(model=model, key=key)
    for attempt in range(retries):
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                res = json.load(r)
            break
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            # 429(요청 한도)는 무료 등급에서 흔하다 — 기다렸다 다시 친다.
            if e.code in (429, 500, 503) and attempt < retries - 1:
                time.sleep(8 * (attempt + 1))
                continue
            raise SystemExit(f"Gemini {e.code}: {detail}")
    part = next((p for c in res.get("candidates", []) for p in c.get("content", {}).get("parts", [])
                 if p.get("inlineData", {}).get("data")), None)
    if not part:
        reason = (res.get("candidates") or [{}])[0].get("finishReason", "?")
        raise NoAudio(f"오디오 없음 (finishReason={reason})")
    inline = part["inlineData"]
    rate = int(m.group(1)) if (m := re.search(r"rate=(\d+)", inline.get("mimeType", ""))) else 24000
    return base64.b64decode(inline["data"]), rate


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cues", default="docs/cues.json")
    ap.add_argument("--out", default="wav")
    ap.add_argument("--model", default=os.environ.get("GEMINI_TTS_MODEL", "gemini-2.5-flash-preview-tts"))
    ap.add_argument("--voices", default=os.environ.get("GEMINI_TTS_VOICE", "Charon"),
                    help="쉼표 구분. 두 개 이상이면 목소리별 폴더로 나눠 저장한다(시청용).")
    ap.add_argument("--only", default="", help="특정 컷만 (예: 1,2,3)")
    ap.add_argument("--delay", type=float, default=5.0,
                    help="컷 사이 간격(초). 무료 등급의 분당 요청 한도를 피한다.")
    ap.add_argument("--skip-existing", action="store_true", help="이미 만든 wav 는 건너뛴다")
    args = ap.parse_args()

    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise SystemExit("GEMINI_API_KEY 가 없습니다")

    cues = json.load(open(args.cues, encoding="utf-8"))
    tone, lines = cues["tone"], cues["lines"]
    if args.only:
        want = {int(x) for x in args.only.split(",") if x.strip()}
        lines = [l for l in lines if l["id"] in want]

    voices = [v.strip() for v in args.voices.split(",") if v.strip()]
    report = []
    for voice in voices:
        outdir = os.path.join(args.out, voice) if len(voices) > 1 else args.out
        os.makedirs(outdir, exist_ok=True)
        print(f"\n■ {voice} · {args.model} · {len(lines)}컷")
        over, failed = [], []
        for i, l in enumerate(lines):
            path = os.path.join(outdir, f"{l['id']:02d}.wav")
            if args.skip_existing and os.path.exists(path):
                print(f"  {l['id']:>2} 건너뜀 (이미 있음)")
                continue
            if i:
                time.sleep(args.delay)
            try:
                pcm, rate = synth(l["text"], voice, args.model, key, tone)
            except NoAudio:
                # 문장이 짧고 끝맺음이 없으면 모델이 낭독 대신 대답을 하려 든다.
                time.sleep(args.delay)
                try:
                    pcm, rate = synth(l["text"].rstrip("."), voice, args.model, key,
                                      tone + ". 아래 문장을 그대로 소리 내어 읽어라")
                except NoAudio as e:
                    print(f"  {l['id']:>2} 실패 — {e}  {l['text']}")
                    failed.append(l["id"])
                    continue
            dur = len(pcm) / (rate * 2)
            with open(path, "wb") as f:
                f.write(wav_from_pcm(pcm, rate))
            slot = l["end"] - l["start"]
            margin = slot - dur
            flag = "초과" if margin < 0 else ("빠듯" if margin < 0.2 else "OK")
            if margin < 0:
                over.append((l["id"], round(-margin, 2), l["text"]))
            print(f"  {l['id']:>2} {dur:>5.2f}초 / 슬롯 {slot:>4.1f}초  {margin:>+5.2f}  {flag}  {l['text']}")
            report.append({"voice": voice, "id": l["id"], "sec": round(dur, 2),
                           "slot": round(slot, 2), "margin": round(margin, 2), "text": l["text"]})
        if failed:
            print(f"\n  ⚠ 생성 실패 {len(failed)}컷: {failed} — --skip-existing 으로 다시 돌리세요")
        if over:
            print(f"\n  ⚠ 슬롯 초과 {len(over)}컷 — 문장을 줄여야 합니다")
            for i, sec, t in over:
                print(f"    {i:>2}번  +{sec}초  {t}")
        else:
            print("\n  ✅ 전 컷이 슬롯 안에 들어갑니다")

    with open(os.path.join(args.out, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print(f"\n리포트: {os.path.join(args.out, 'report.json')}")


if __name__ == "__main__":
    main()
