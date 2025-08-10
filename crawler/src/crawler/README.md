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