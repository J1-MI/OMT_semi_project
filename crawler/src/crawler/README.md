# Dark Web Crawler (Playwright + Tor)

이 크롤러는 Kali Linux 환경에서 Tor 네트워크를 통해 .onion 웹사이트에 접근하여 HTML과 스크린샷을 저장합니다.  
OSINT 및 보안 연구 목적에만 사용하세요.

## 📦 설치 방법

### 1. 레포지토리 클론
git clone https://github.com/J1-MI/OMT_semi_project.git
cd OMT_semi_project/crawler

2. 가상환경 생성 및 패키지 설치
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip wheel setuptools
pip install -r requirements.txt

3. Playwright 브라우저 설치
python -m playwright install chromium
python -m playwright install-deps

4. Tor 설치 및 실행
sudo apt -y install tor
sudo systemctl enable --now tor

🚀 실행 방법
cd src
python crawl_one.py http://duckduckgogg42xjoc72x3sjasowoarfbgcmvfimaftt6twagswzczad.onion
크롤링 결과는 crawler/out/ 폴더에 HTML과 PNG 파일로 저장됩니다.

--------

## crawl_one.py 사용방법
1) 필수 환경
Python 3.10+ (3.11 권장)

OS: Windows / macOS / Linux 아무거나

(선택) Tor 프록시

requests용: 127.0.0.1:9150 (Tor Browser 기본)

Playwright용: 127.0.0.1:9050 (tor 서비스 기본; 필요 시 옵션으로)

2) 설치
# 새 가상환경(권장)
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

# 필수 설치
pip install "requests[socks]" beautifulsoup4 lxml pyyaml

# (옵션) SQLite 쓰려면 표준 라이브러리 sqlite3 있으면 됨 (대부분 기본 내장)

# Playwright(선택: JS 렌더링 필요 포럼 지원)
pip install playwright
python -m playwright install chromium

# (옵션) ZIP 암호화 쓰고 싶으면
pip install pyminizip

3) 셀렉터 selectors.yaml
  darkforums:
  engine: requests                   # 또는 playwright
  list_urls:
    - "https://example.onion/forum?page=1"
  thread_link: ["a.thread-title", "h3.title > a"]
  next_page: "a.next"

  thread_title: ["h1.thread-title", "h1.title"]
  post_container: ["div.post", "li.comment"]
  content: ["div.content", "div.body"]
  author: ["span.author", "a.user"]
  posted_time: ["time", "span.time"]
  post_permalink: ["a.permalink"]

  attachment_block: ["div.attach", "li.attachment"]
  attachment_name: ["span.filename", "a.file"]
  attachment_size: ["span.size"]

포럼이 여러 개면 darkforums, myjsforum 처럼 키를 추가해서 나열하면 됨
engine 없으면 전역 --engine 값이 적용됨.

4) 기본 크롤링 실행 (HTML만 수집)
  python crawler_merged.py \
  --config selectors.yaml \
  --forums darkforums myjsforum \
  --engine auto \
  --pages 2

--engine auto : 각 포럼 키의 engine 값을 따르되, 없으면 기본 requests.
결과: out/crawl_YYYYMMDD_HHMMSS.jsonl 생성 (+ 옵션으로 SQLite)

5) Tor 통해서 돌리기 (권장)
  # Tor 브라우저 켜서 9150 열어두거나, tor 서비스로 9050/9150 오픈
  python crawler_merged.py \
  --config selectors.yaml \
  --forums darkforums myjsforum \
  --engine auto \
  --pages 2 \
  --tor \
  --tor-requests-port 9150 \
  --tor-playwright-port 9050

6) 첨부 격리 다운로드(옵션: 기본 OFF)
첨부파일을 실행하지 않고 .quarantine 확장자로 저장 + manifest 기록.
  python crawler_merged.py \
  --config selectors.yaml \
  --forums darkforums \
  --engine auto --pages 2 --tor \
  --download-attachments \
  --dl-out quarantine \
  --dl-max-size 20000000 \
  --dl-max-per-thread 3 \
  --dl-same-host \
  --zip-quarantine \
  --zip-password infected   # pyminizip 있을 때만 적용

--dl-same-host : 스레드와 동일 호스트의 첨부만 받기(피싱/외부유도 차단용).
VM 분석 시: .quarantine 확장자를 지우고 복원해서 쓰거나(mv a.exe.quarantine a.exe), 정적 도구(strings/YARA/해시)는 확장자 그대로도 됨.

7) 출력물 확인
JSONL
# 맥/리눅스
head -n 1 out/crawl_*.jsonl | python -m json.tool | sed -n '1,80p'

SQLite(옵션)
python crawler_merged.py --config selectors.yaml --forums darkforums --out-sqlite out/crawl.db
sqlite3 out/crawl.db "select count(*), min(posted_at), max(posted_at) from posts;"
sqlite3 out/crawl.db "select title, author, substr(content,1,120) from posts limit 5;"

## 9) 안전 가이드**
기본 크롤링은 첨부 다운로드 안 함(메타데이터만).
파일이 필요할 때만 --download-attachments.
저장 파일은 .quarantine 확장자라 더블클릭해도 실행 안 됨.
분석은 반드시 VM 스냅샷 상태에서 진행. 자동 미리보기/자동 압축해제 꺼두기.

10) 트러블슈팅
Playwright 에러: playwright install chromium 재실행. 프록시(9050) 안 열렸으면 --tor-playwright-port 확인.
Tor 연결 느림/차단: 페이지 타임아웃 --pages 줄이고 재시도. 혹은 일시적으로 --tor 끄고 구조만 점검.
셀렉터가 안 맞음: selectors.yaml의 CSS 수정. thread_link/post_container부터 최소 단위로 맞춰.
대용량 응답: 기본 크기 제한이 있으니(HTML fetch / DL 둘 다), 페이지가 너무 크면 페이지 수를 줄이거나, 코드에서 max_bytes 상향 조절.
