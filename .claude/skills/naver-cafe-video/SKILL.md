---
name: naver-cafe-video
description: 네이버 카페 게시글에 첨부된 영상을 원본 화질로 일괄 다운로드한다. 사용자가 "카페 영상 받아줘", "869번 글 영상 다 받아줘", "네이버 카페 강의 저장해줘", "영상 다운로드" 처럼 네이버 카페의 영상을 저장해 달라고 하면 이 스킬을 사용할 것. 글 번호만 말해도 적용한다.
---

# 네이버 카페 영상 일괄 다운로드

화면 녹화가 아니라 네이버가 내려주는 **원본 스트림을 직접 받는다.** 15분짜리
영상도 수십 초면 끝나고, 재생 버튼을 누를 필요가 없다.

## 전제

이 스킬은 **사용자 PC에서 실행되는 세션에서만 동작한다.** 클라우드 세션은
naver.com 이 차단되어 있고 로그인 세션도 없다. `pwd` 가 `/home/user/...` 로
나오면 클라우드이므로, 사용자에게 로컬 세션으로 열어야 한다고 알릴 것.

## 실행 순서

1. **로그인 상태 확인.** 아래 경로에 파일이 없으면 로그인부터 해야 한다.
   - Windows: `%LOCALAPPDATA%\naver_cafe_dl\state.json`
   - 그 외: `~/.config/naver_cafe_dl/state.json`

2. **없으면 로그인** (최초 1회. 브라우저가 뜨고 사용자가 직접 로그인한다):
   ```
   python scripts/naver_cafe_dl.py login
   ```
   사용자에게 "브라우저에서 네이버 로그인 후 터미널에서 Enter" 라고 안내할 것.
   이 명령은 사용자 입력을 기다리므로 절대 백그라운드로 돌리지 말 것.

3. **먼저 목록만 확인** (글 번호만으로 충분하다):
   ```
   python scripts/naver_cafe_dl.py grab 869 --list
   ```

4. `영상 블록 N개` 에서 **N이 1 이상이면 실제 다운로드**:
   ```
   python scripts/naver_cafe_dl.py grab 869
   ```
   **바탕화면의 `네이버카페영상` 폴더**에 `01_입문 6,7강 (1).mp4` 형태로 저장된다.
   다른 곳에 받고 싶다고 하면 `--out <폴더>` 를 붙인다.

## 의존성

없으면 설치한다. Chromium 은 `playwright install` 을 따로 해야 한다.

```
pip install playwright requests
playwright install chromium
```

## 문제 해결

| 증상 | 원인과 조치 |
|---|---|
| `영상 블록 0개` | 로그인 만료 또는 페이지 구조 변경. `_debug/modules.json` 을 읽고 `collect_from_dom()` 의 키 이름을 맞출 것 |
| 로그인 벽 화면 | `login` 을 다시 실행해 세션 갱신 |
| `Executable doesn't exist` | `playwright install chromium` 누락 |
| HLS 영상 | ffmpeg 필요. 스크립트가 자동 감지해 알려준다 |

## 주의

- `state.json` 에는 네이버 세션 쿠키가 들어 있다. **저장소에 커밋하지 말 것.**
  경로를 저장소 바깥에 둔 이유가 이것이다.
- 다른 카페는 `--cafe <ID>` 로 지정한다. 기본값은 31568077.
- 받은 영상은 개인 소장용이다. 재배포하지 않는다.
