import os, sys, subprocess, re, glob

def run_crawler(
    config_path="crawler/src/crawler/selectors.yaml",
    forums=("darkforums",),           # selectors.yaml의 키
    pages=1,
    engine="requests",                # "requests"|"playwright"|"auto"
    use_tor=False,
    out_jsonl="data/crawled/crawl.jsonl",
    out_sqlite=None,
):
    """
    통합 크롤러(crawl_one.py)를 서브프로세스로 실행.
    결과 JSONL은 프로젝트 표준 ./data/crawled/ 아래로 저장(스크립트가 타임스탬프를 붙임).
    """
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    script = os.path.join(root, "crawler", "src", "crawler", "src", "crawl_one.py")
    os.makedirs(os.path.join(root, "data", "crawled"), exist_ok=True)

    cmd = [
        sys.executable, script,
        "--config", os.path.join(root, config_path),
        "--forums", *forums,
        "--pages", str(pages),
        "--engine", engine,
        "--out-jsonl", os.path.join(root, out_jsonl),
    ]
    if use_tor:
        cmd.append("--tor")
    if out_sqlite:
        cmd += ["--out-sqlite", os.path.join(root, out_sqlite)]

    proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True)

    # [OK] JSONL saved: <path> (...) 로그에서 경로 추출
    m = re.search(r"\[OK\]\s+JSONL saved:\s+(.+?)\s+\(", proc.stdout)
    saved = m.group(1).strip() if m else None
    if not saved:
        # 경로 못 찾으면 최신 파일 스캔
        candidates = sorted(glob.glob(os.path.join(root, "data", "crawled", "crawl_*.jsonl")))
        if candidates:
            saved = candidates[-1]

    return {
        "ok": proc.returncode == 0 and bool(saved),
        "out_jsonl": saved,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "cmd": " ".join(cmd),
    }
