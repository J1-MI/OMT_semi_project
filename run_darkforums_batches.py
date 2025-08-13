# -*- coding: utf-8 -*-
"""
DarkForums 대량 배치 러너
- 페이지를 CHUNK 단위(예: 200)로 나눠 여러 번 크롤
- 각 배치마다 시작 페이지를 반영한 임시 selectors.yaml 생성
- 실패 시 재시도/백오프, 계속 실패하면 engine을 playwright로 승급
- 필요시 Tor 회로 재설정 훅 제공(주석)
"""

import subprocess, sys, time, random, shutil
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parent
CRAWLER_DIR = ROOT / "crawler" / "src" / "crawler" / "src"
CRAWL_ONE = CRAWLER_DIR / "crawl_one.py"
BASE_CFG = CRAWLER_DIR / "selectors.yaml"
OUT_DIR = ROOT / "crawler" / "out"

FORUMS = "darkforums"              # --forums 인자
MAX_PAGE = 800                     # 전체 목표 페이지 수
CHUNK = 200                        # 배치 크기(한 번에 몇 페이지)
TOR_REQ_PORT = 9150                # --tor-requests-port
PLAY_PORT = 9150                   # --tor-playwright-port
TIMEOUT_SEC = 60                   # (엔진 내부 타임아웃은 코드쪽, 여기선 백오프만)

# Playwright를 쓸 수도 있으니 설치 확인 필요:
#   pip install playwright
#   python -m playwright install chromium

def rand_sleep(a=1.5, b=3.0):
    t = random.uniform(a, b)
    time.sleep(t)

def build_temp_config(start_page: int) -> Path:
    """
    시작 페이지를 list_urls에 반영한 임시 selectors.yaml 생성
    ex) https://darkforums.st/Forum-Databases?page=401
    """
    cfg = yaml.safe_load(BASE_CFG.read_text(encoding="utf-8"))
    d = cfg.get("darkforums") or {}

    urls = d.get("list_urls") or []
    new_urls = []
    for u in urls:
        sep = "&" if "?" in u else "?"
        new_urls.append(f"{u}{sep}page={start_page}")

    d["list_urls"] = new_urls
    cfg["darkforums"] = d

    tmp = OUT_DIR / f"selectors.start{start_page}.yaml"
    tmp.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return tmp

def run_crawl(temp_cfg: Path, pages: int, engine: str) -> bool:
    """
    crawl_one.py 실행. 실패 시 False
    """
    out_jsonl = OUT_DIR / f"{FORUMS}_{engine}_{pages}.jsonl"
    cmd = [
        sys.executable, str(CRAWL_ONE),
        "--config", str(temp_cfg),
        "--forums", FORUMS,
        "--pages", str(pages),
        "--engine", engine,
        "--tor",
        "--tor-requests-port", str(TOR_REQ_PORT),
        "--tor-playwright-port", str(PLAY_PORT),
        "--out-jsonl", str(out_jsonl),
        # 필요 시 다운로드 옵션:
        # "--download-attachments", "--dl-out", str(OUT_DIR/"dl")
    ]
    print(">>", " ".join(cmd), flush=True)
    try:
        proc = subprocess.run(cmd, cwd=str(CRAWLER_DIR), capture_output=True, text=True, timeout=pages*TIMEOUT_SEC)
        sys.stdout.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        return proc.returncode == 0
    except subprocess.TimeoutExpired:
        print(f"[WARN] crawl timeout (engine={engine})", flush=True)
        return False

def tor_newnym():
    """
    Tor 회로 재설정 훅 (환경에 맞게 구현하세요)
    - 윈도우: service 재시작 스크립트 호출 등
    - Linux: `sudo systemctl reload tor` 또는 Stem 라이브러리 사용
    """
    # 예시:
    # subprocess.run(["powershell", "-Command", "Restart-Service tor"], check=False)
    pass

def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    chunks = []
    for start in range(1, MAX_PAGE+1, CHUNK):
        chunks.append((start, min(CHUNK, MAX_PAGE - start + 1)))

    produced = []
    for (start, pages) in chunks:
        print(f"\n==== BATCH start={start} pages={pages} ====")

        tmp_cfg = build_temp_config(start)
        success = False

        for attempt in range(1, 4):   # requests 엔진 최대 3회 시도
            ok = run_crawl(tmp_cfg, pages, engine="requests")
            if ok:
                success = True
                break
            print(f"[WARN] requests attempt {attempt} failed; backoff…", flush=True)
            rand_sleep(5, 10)
            tor_newnym()

        if not success:
            print("[INFO] escalating to playwright", flush=True)
            ok = run_crawl(tmp_cfg, pages, engine="playwright")
            if not ok:
                print(f"[ERR] playwright also failed for start={start}", flush=True)
                continue

        produced.append((start, pages))
        rand_sleep(1.5, 3.0)

    # (선택) 여기서 postproc_darkforums.py로 병합/중복제거/요약 실행 가능
    print(f"\n[INFO] finished {len(produced)} batches")

if __name__ == "__main__":
    main()
