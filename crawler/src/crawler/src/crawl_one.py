"""
통합 포럼 크롤러
- crawler_min.py 코드(requests+BeautifulSoup 기반)의 안전장치/셀렉터/YAML/저장(JSONL, SQLite) 유지
- 기존 코드(Playwright 기반)를 엔진으로 추가해 JS 의존 사이트 대응
- 포럼별로 engine 선택 가능(config selectors.yaml에 engine 키 추가)
- 공통 스키마(thread -> posts -> attachments 메타데이터만, 파일 자체는 비수집)
- TOR 프록시 지원: requests는 socks5h://127.0.0.1:9150, playwright는 socks5://127.0.0.1:9050 기본값(필요 시 옵션으로 변경)

사용 예시
---------
$ python crawler_merged.py --config selectors.yaml --forums darkforums myjsforum \
    --pages 2 --out-jsonl out/crawl.jsonl --out-sqlite out/crawl.db \
    --engine auto --tor

selectors.yaml 예시 항목 추가
-----------------------------
myjsforum:
  engine: playwright   # 없으면 전역 --engine 또는 기본값 사용(requests)
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

주의
----
- 실제 플레이북 의존 설치 필요: `pip install playwright && playwright install chromium`
- requests용 TOR는 9150(브라우저 번들), playwright는 9050(tor service) 기본값으로 가정. 환경에 맞게 옵션으로 수정 가능
"""

from __future__ import annotations
import argparse, json, os, re, sys, time, hashlib, random
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional, Tuple, Dict, Any
from urllib.parse import urljoin

# --- Optional deps ---
try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except Exception:
    requests = None
    HTTPAdapter = None
    Retry = None

try:
    from bs4 import BeautifulSoup
except Exception:
    BeautifulSoup = None

try:
    import yaml
except Exception:
    yaml = None

try:
    import sqlite3
except Exception:
    sqlite3 = None

# playwright는 선택적으로 로드
_playwright_available = True
try:
    from playwright.sync_api import sync_playwright
except Exception:
    _playwright_available = False

# -------------------- CONST & SAFE GUARDS --------------------
UA = {"User-Agent": "Mozilla/5.0 (compatible; DarkWatch/1.0)"}
HTML_CT_ALLOWED = ("text/html", "application/xhtml+xml")
ATTACHMENT_URL_PATTERN = re.compile(
    r"(?i)(/attachment|/attachments|/upload|/uploads|/files|/download)"
    r"|(\.(zip|7z|rar|exe|iso|apk|jar|bat|ps1|dll|scr|docm|xlsm|pdf|gz|bz2|xz)(?:$|\?))"
)


def is_attachment_url(url: str) -> bool:
    return bool(ATTACHMENT_URL_PATTERN.search(url or ""))


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


# -------------------- ENGINE ABSTRACTION --------------------
class FetchError(RuntimeError):
    pass


@dataclass
class FetchResult:
    url: str
    html: str


class BaseEngine:
    def fetch_html(self, url: str, *, timeout: int = 30, max_bytes: int = 3_000_000) -> FetchResult:
        raise NotImplementedError

    # 엔진별 마너 슬립
    def human_sleep(self, low: float = 1.0, high: float = 2.0) -> None:
        time.sleep(random.uniform(low, high))


class RequestsEngine(BaseEngine):
    def __init__(self, proxies: Optional[Dict[str, str]] = None):
        if requests is None or HTTPAdapter is None or Retry is None:
            raise RuntimeError("requests 관련 의존성이 설치되어 있지 않습니다.")
        self.session = requests.Session()
        self.session.headers.update(UA)
        retry = Retry(
            total=3,
            backoff_factor=1.2,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        if proxies:
            self.session.proxies.update(proxies)

    def _safe_get_html(self, url: str, *, timeout: int, max_bytes: int) -> str:
        if is_attachment_url(url):
            raise FetchError(f"Blocked attachment-like URL: {url}")
        current = url
        for _ in range(5):  # redirect depth
            r = self.session.get(current, timeout=timeout, allow_redirects=False, stream=True)
            try:
                if 300 <= r.status_code < 400 and "Location" in r.headers:
                    nxt = urljoin(current, r.headers["Location"])
                    if is_attachment_url(nxt):
                        raise FetchError(f"Redirected to blocked URL: {nxt}")
                    current = nxt
                    r.close()
                    continue

                cd = (r.headers.get("Content-Disposition") or "").lower()
                if "attachment" in cd:
                    raise FetchError("Server suggests attachment download.")
                ct = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if ct not in HTML_CT_ALLOWED:
                    raise FetchError(f"Non-HTML Content-Type: {ct or 'unknown'}")

                cl = r.headers.get("Content-Length")
                if cl and cl.isdigit() and int(cl) > max_bytes:
                    raise FetchError(f"Response too large: {cl} bytes")

                chunks, total = [], 0
                for chunk in r.iter_content(8192):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        raise FetchError(f"Exceeded {max_bytes} bytes")
                    chunks.append(chunk)
                raw = b"".join(chunks)
                enc = r.encoding or "utf-8"
                try:
                    return raw.decode(enc, errors="replace")
                except LookupError:
                    return raw.decode("utf-8", errors="replace")
            finally:
                r.close()
        raise FetchError("Too many redirects")

    def fetch_html(self, url: str, *, timeout: int = 30, max_bytes: int = 3_000_000) -> FetchResult:
        html = self._safe_get_html(url, timeout=timeout, max_bytes=max_bytes)
        return FetchResult(url=url, html=html)


class PlaywrightEngine(BaseEngine):
    def __init__(self, *, tor_socks: Optional[str] = None, headless: bool = True):
        if not _playwright_available:
            raise RuntimeError("playwright가 설치되어 있지 않습니다. `pip install playwright && playwright install chromium`")
        self.tor_socks = tor_socks
        self.headless = headless
        self._p = None
        self._browser = None

    def __enter__(self):
        self._p = sync_playwright().start()
        args = [
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--ignore-certificate-errors",
            "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1",
            "--proxy-bypass-list=<-loopback>",
        ]
        proxy = None
        if self.tor_socks:
            proxy = {"server": f"socks5://{self.tor_socks}"}
        self._browser = self._p.chromium.launch(headless=self.headless, proxy=proxy, args=args)
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if self._browser:
                self._browser.close()
        finally:
            if self._p:
                self._p.stop()

    def fetch_html(self, url: str, *, timeout: int = 30, max_bytes: int = 3_000_000) -> FetchResult:
        if is_attachment_url(url):
            raise FetchError(f"Blocked attachment-like URL: {url}")
        if self._browser is None:
            raise RuntimeError("PlaywrightEngine must be used as a context manager")

        page = self._browser.new_page()
        page.set_default_timeout(timeout * 1000)
        try:
            page.set_extra_http_headers(UA)
            page.goto(url, wait_until="domcontentloaded")
            # JS 무거운 사이트 고려해 추가 대기
            page.wait_for_timeout(2000)

            # content-type 검사 유사 처리: 응답 헤더 접근 제한적이라, 문서 타입 간단 체크
            html = page.content()
            # 크기 제한
            if len(html.encode("utf-8")) > max_bytes:
                raise FetchError(f"Exceeded {max_bytes} bytes")
            return FetchResult(url=url, html=html)
        finally:
            page.close()


# -------------------- SELECT & PARSE --------------------
def _pick_one(soup_or_node, candidates, many=False):
    if not candidates:
        return ([], None) if many else (None, None)
    if isinstance(candidates, str):
        candidates = [candidates]
    for css in candidates:
        if not css:
            continue
        try:
            found = soup_or_node.select(css)
        except Exception:
            found = []
        if found:
            return (found, css) if many else (found[0], css)
    return ([], None) if many else (None, None)


def _parse_thread(thread_url: str, sel: dict, fetch_html_fn) -> dict:
    res = fetch_html_fn(thread_url)
    s = BeautifulSoup(res.html, "lxml")

    title_el, _ = _pick_one(s, sel["thread_title"])
    thread_title = title_el.get_text(" ", strip=True) if title_el else None

    nodes, _ = _pick_one(s, sel["post_container"], many=True)
    posts = []
    for node in nodes:
        content_el, _ = _pick_one(node, sel["content"])
        content = content_el.get_text(" ", strip=True) if content_el else ""

        author_el, _ = _pick_one(node, sel["author"])
        author = author_el.get_text(strip=True) if author_el else None

        time_el, _ = _pick_one(node, sel["posted_time"])
        if time_el and time_el.has_attr("datetime"):
            posted_at = time_el["datetime"]
        else:
            posted_at = time_el.get_text(" ", strip=True) if time_el else utcnow_iso()

        pl_el, _ = _pick_one(node, sel.get("post_permalink"))
        post_url = urljoin(thread_url, pl_el.get("href")) if pl_el and pl_el.get("href") else thread_url

        attachments = []
        att_nodes, _ = _pick_one(node, sel.get("attachment_block"), many=True)
        for a in att_nodes:
            name_el, _ = _pick_one(a, sel.get("attachment_name"))
            size_el, _ = _pick_one(a, sel.get("attachment_size"))
            alink = a.find("a") if hasattr(a, "find") else None
            att_url = urljoin(thread_url, alink.get("href")) if (alink and alink.get("href")) else None
            if att_url and not is_attachment_url(att_url):
                # 메타데이터만 기록(파일 다운로드 금지)
                attachments.append({
                    "display_filename": name_el.get_text(strip=True) if name_el else None,
                    "display_size": size_el.get_text(strip=True) if size_el else None,
                    "attachment_url": att_url,
                })

        posts.append({
            "post_url": post_url,
            "author": author,
            "posted_at": posted_at,
            "content": content,
            "attachments": attachments,
        })

    return {
        "thread_url": thread_url,
        "title": thread_title,
        "posts": posts,
        "fetched_at": utcnow_iso(),
        "thread_hash": sha256((thread_title or "") + "".join(p["content"] for p in posts)[:5000]),
    }


def crawl_forum(key: str, conf: dict, engine: BaseEngine, *, max_pages=2, sleep_sec=(1.0, 2.0)):
    sel = conf[key]
    visited_threads = set()
    results = []

    list_urls = sel.get("list_urls", [])
    thread_link_cands = sel.get("thread_link")
    next_page_sel = sel.get("next_page")

    def fetch(url: str):
        return engine.fetch_html(url)

    for start_url in list_urls:
        next_url, page = start_url, 1
        while next_url and page <= max_pages:
            html = fetch(next_url).html
            s = BeautifulSoup(html, "lxml")

            # collect thread links
            link_nodes: List[Any] = []
            for cand in (thread_link_cands if isinstance(thread_link_cands, list) else [thread_link_cands]):
                try:
                    link_nodes = s.select(cand)
                except Exception:
                    link_nodes = []
                if link_nodes:
                    break

            seen_on_page = set()
            for a in link_nodes:
                href = a.get("href") if hasattr(a, "get") else None
                if not href:
                    continue
                thread_url = urljoin(next_url, href)
                if thread_url in seen_on_page or thread_url in visited_threads:
                    continue
                if is_attachment_url(thread_url):
                    continue
                seen_on_page.add(thread_url)
                visited_threads.add(thread_url)

                try:
                    data = _parse_thread(thread_url, sel, fetch)
                    results.append({"source": key, **data})
                except Exception as e:
                    print(f"[WARN] parse_thread failed: {thread_url} ({e})", file=sys.stderr)

                engine.human_sleep(*sleep_sec)

            np_el, _ = _pick_one(s, next_page_sel)
            next_url = urljoin(next_url, np_el.get("href")) if np_el and np_el.get("href") else None
            page += 1
            engine.human_sleep(*sleep_sec)
    return results


# -------------------- SAVE --------------------
def save_jsonl(records, path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"[OK] JSONL saved: {path} ({len(records)} records)")


def save_sqlite(records, path):
    if sqlite3 is None:
        print("[SKIP] sqlite3 unavailable.", file=sys.stderr)
        return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS posts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source TEXT, thread_url TEXT, post_url TEXT,
          title TEXT, author TEXT, posted_at TEXT,
          content TEXT, fetched_at TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS attachments (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          post_id INTEGER,
          display_filename TEXT, display_size TEXT, attachment_url TEXT,
          FOREIGN KEY(post_id) REFERENCES posts(id)
        )
        """
    )
    for rec in records:
        source = rec["source"]
        thread_url = rec["thread_url"]
        title = rec.get("title")
        fetched_at = rec.get("fetched_at")
        for p in rec["posts"]:
            cur.execute(
                """
                INSERT INTO posts(source,thread_url,post_url,title,author,posted_at,content,fetched_at)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    source,
                    thread_url,
                    p["post_url"],
                    title,
                    p.get("author"),
                    p.get("posted_at"),
                    p.get("content"),
                    fetched_at,
                ),
            )
            pid = cur.lastrowid
            for a in p.get("attachments", []):
                cur.execute(
                    """
                    INSERT INTO attachments(post_id,display_filename,display_size,attachment_url)
                    VALUES(?,?,?,?)
                    """,
                    (
                        pid,
                        a.get("display_filename"),
                        a.get("display_size"),
                        a.get("attachment_url"),
                    ),
                )
    conn.commit()
    conn.close()
    print(f"[OK] SQLite saved: {path}")


# -------------------- CONFIG & MAIN --------------------
def load_config_smart(path_str: str) -> dict:
    if yaml is None:
        raise RuntimeError("pyyaml이 설치되어 있지 않습니다.")
    cand = Path(path_str)
    if cand.exists():
        return yaml.safe_load(cand.read_text(encoding="utf-8"))
    here = Path(__file__).parent / path_str
    if here.exists():
        return yaml.safe_load(here.read_text(encoding="utf-8"))
    raise FileNotFoundError(f"selectors not found: {path_str}")


def make_engine(name: str, *, use_tor: bool, tor_requests_port: int, tor_playwright_port: int) -> BaseEngine:
    if name == "requests":
        proxies = None
        if use_tor:
            proxies = {
                "http": f"socks5h://127.0.0.1:{tor_requests_port}",
                "https": f"socks5h://127.0.0.1:{tor_requests_port}",
            }
            print(f"[INFO] requests+TOR via socks5h://127.0.0.1:{tor_requests_port}")
        return RequestsEngine(proxies=proxies)
    elif name == "playwright":
        tor = f"127.0.0.1:{tor_playwright_port}" if use_tor else None
        print(
            f"[INFO] playwright chromium (headless) with TOR {tor if tor else 'disabled'}"
        )
        return PlaywrightEngine(tor_socks=tor, headless=True)
    else:
        raise ValueError(f"unknown engine: {name}")


def main():
    ap = argparse.ArgumentParser(description="Merged forum crawler (requests + playwright)")
    ap.add_argument("--config", default="selectors.yaml")
    ap.add_argument("--forums", nargs="+", default=["darkforums"])
    ap.add_argument("--pages", type=int, default=2, help="pages per list")
    ap.add_argument(
        "--engine",
        default="auto",
        choices=["auto", "requests", "playwright"],
        help="default engine; 'auto' uses per-forum config if present",
    )
    ap.add_argument("--tor", action="store_true", help="use TOR socks proxy")
    ap.add_argument("--tor-requests-port", type=int, default=9150)
    ap.add_argument("--tor-playwright-port", type=int, default=9050)
    ap.add_argument("--out-jsonl", default="out/crawl.jsonl")
    ap.add_argument("--out-sqlite", default=None, help="optional SQLite path")

    # ---- Quarantine downloader options (opt-in) ----
    ap.add_argument("--download-attachments", action="store_true",
                    help="[DANGEROUS] Download attachments to quarantine (opt-in, never execute)")
    ap.add_argument("--dl-out", default="quarantine",
                    help="quarantine base directory")
    ap.add_argument("--dl-max-size", type=int, default=20_000_000,
                    help="max bytes per file (default 20MB)")
    ap.add_argument("--dl-max-per-thread", type=int, default=5,
                    help="max files per thread to download")
    ap.add_argument("--dl-same-host", action="store_true",
                    help="only download if attachment host equals thread host")
    ap.add_argument("--zip-quarantine", action="store_true",
                    help="create a zip archive of each thread's quarantine folder")
    ap.add_argument("--zip-password", default=None,
                    help="password for zip (requires pyminizip; ignored if unavailable)")

    args = ap.parse_args()

    try:
        conf = load_config_smart(args.config)
    except Exception as e:
        print(f"[ERR] load config failed: {e}", file=sys.stderr)
        sys.exit(2)

    all_records = []

    # 엔진 재사용: requests는 전역 하나, playwright는 with-context 필요
    requests_engine: Optional[RequestsEngine] = None

    def get_engine_for_forum(key: str) -> BaseEngine:
        nonlocal requests_engine
        forum_engine = conf.get(key, {}).get("engine")
        chosen = (
            forum_engine if args.engine == "auto" and forum_engine in {"requests", "playwright"} else args.engine
        )
        if chosen == "requests" or chosen == "auto":
            if requests_engine is None:
                requests_engine = make_engine(
                    "requests",
                    use_tor=args.tor,
                    tor_requests_port=args.tor_requests_port,
                    tor_playwright_port=args.tor_playwright_port,
                )
            return requests_engine
        else:
            return make_engine(
                "playwright",
                use_tor=args.tor,
                tor_requests_port=args.tor_requests_port,
                tor_playwright_port=args.tor_playwright_port,
            )

    # playwright 필요 여부 확인
    need_playwright = any(
        (args.engine == "playwright") or (args.engine == "auto" and conf.get(k, {}).get("engine") == "playwright")
        for k in args.forums
    )

    if need_playwright:
        with PlaywrightEngine(
            tor_socks=(f"127.0.0.1:{args.tor_playwright_port}" if args.tor else None), headless=True
        ) as shared_pw:
            for key in args.forums:
                if key not in conf:
                    print(f"[WARN] forum key not in config: {key}", file=sys.stderr)
                    continue
                print(f"[INFO] Crawling: {key}")
                try:
                    eng = shared_pw if (args.engine == "playwright" or conf.get(key, {}).get("engine") == "playwright") else get_engine_for_forum(key)
                    recs = crawl_forum(key, conf, eng, max_pages=args.pages)
                    all_records.extend(recs)
                except Exception as e:
                    print(f"[WARN] crawl_forum failed: {key} ({e})", file=sys.stderr)
    else:
        for key in args.forums:
            if key not in conf:
                print(f"[WARN] forum key not in config: {key}", file=sys.stderr)
                continue
            print(f"[INFO] Crawling: {key}")
            try:
                eng = get_engine_for_forum(key)
                recs = crawl_forum(key, conf, eng, max_pages=args.pages)
                all_records.extend(recs)
            except Exception as e:
                print(f"[WARN] crawl_forum failed: {key} ({e})", file=sys.stderr)

    if not all_records:
        print("[DONE] no records.")
        return

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = args.out_jsonl[:-6] if args.out_jsonl.endswith(".jsonl") else args.out_jsonl
    jsonl_path = f"{base}_{ts}.jsonl"
    save_jsonl(all_records, jsonl_path)

    if args.out_sqlite:
        sqlite_path = args.out_sqlite if args.out_sqlite.endswith(".db") else args.out_sqlite + ".db"
        save_sqlite(all_records, sqlite_path)

    # ---- Optional quarantine download pass ----
    if args.download_attachments:
        print("[INFO] Quarantine download enabled. NEVER EXECUTE downloaded files.")
        qbase = Path(args.dl_out)
        qbase.mkdir(parents=True, exist_ok=True)
        dl_session = make_requests_session_for_dl(args.tor, args.tor_requests_port)
        from urllib.parse import urlparse

        for rec in all_records:
            # collect att urls
            att_urls = []
            for p in rec.get("posts", []):
                for a in p.get("attachments", []):
                    u = a.get("attachment_url")
                    if u: att_urls.append(u)
            if not att_urls:
                continue

            # per-thread folder
            src = sanitize_filename(rec.get("source", "unknown"), "src")
            thash = rec.get("thread_hash") or sha256(rec.get("thread_url", ""))
            tdir = qbase / src / thash
            tdir.mkdir(parents=True, exist_ok=True)

            same_host = None
            if args.dl_same_host:
                same_host = urlparse(rec.get("thread_url", "")).netloc

            manifest = {"thread_url": rec.get("thread_url"), "downloaded": []}
            count = 0
            for u in att_urls:
                if count >= args.dl_max_per_thread:
                    break
                meta = stream_download(u, dl_session, dst_dir=tdir, max_bytes=args.dl_max_size, same_host=same_host)
                if meta:
                    manifest["downloaded"].append(meta)
                    count += 1

            # write manifest
            (tdir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

            if args.zip_quarantine:
                zpath = tdir.with_suffix(".zip")
                zip_quarantine_folder(tdir, zpath, password=args.zip_password)
                print(f"[OK] Quarantine zipped: {zpath}")


if __name__ == "__main__":
    if BeautifulSoup is None:
        print("[ERR] beautifulsoup4가 설치되어 있지 않습니다. `pip install beautifulsoup4 lxml`", file=sys.stderr)
        sys.exit(2)
    main()


# === Quarantine attachment downloader (opt-in, safe-by-default) ===
# 본 섹션은 악성 가능성이 있는 첨부 파일을 "절대 실행하지 않고" 격리 저장하는 목적입니다.
# 기본적으로 비활성화되어 있으며, --download-attachments 플래그를 켜야 동작합니다.
# 안전장치:
#  - 명시적 opt-in (--download-attachments)
#  - HTML/텍스트 응답 차단, 크기 제한(--dl-max-size), 개수 제한(--dl-max-per-thread)
#  - 파일명 정규화 및 무해화(.quarantine 확장자), 해시/메타데이터 manifest 기록
#  - (선택) ZIP 묶기(--zip-quarantine), (선택) 암호 설정(--zip-password, pyminizip가 있을 때만)

import shutil
import zipfile

try:
    import pyminizip  # 선택사항: 비표준 의존성
except Exception:
    pyminizip = None

SAFE_TEXT_CT = {"text/html", "application/xhtml+xml", "text/plain", "text/markdown"}

_name_sanitize = re.compile(r"[^A-Za-z0-9._-]+")

def sanitize_filename(name: str, fallback: str) -> str:
    name = (name or "").strip() or fallback
    name = _name_sanitize.sub("_", name)
    if len(name) > 120:
        root, dot, ext = name.rpartition('.')
        name = (root[:80] + (dot + ext if dot else '')) or name[:120]
    return name


def make_requests_session_for_dl(use_tor: bool, tor_requests_port: int):
    if requests is None:
        raise RuntimeError("requests가 필요합니다.")
    s = requests.Session()
    s.headers.update(UA)
    if use_tor:
        s.proxies.update({
            "http": f"socks5h://127.0.0.1:{tor_requests_port}",
            "https": f"socks5h://127.0.0.1:{tor_requests_port}",
        })
    return s


def stream_download(url: str, session, *, dst_dir: Path, max_bytes: int, same_host: Optional[str]) -> Optional[dict]:
    # same_host: 같은 호스트만 허용할 때 호스트 문자열 전달
    try:
        from urllib.parse import urlparse
        pu = urlparse(url)
        if same_host and pu.netloc and pu.netloc != same_host:
            print(f"[SKIP] cross-host blocked: {url}", file=sys.stderr)
            return None

        # 위험 URL 패턴이더라도 명시적 opt-in 시도는 허용(단, HTML/텍스트는 차단)
        r = session.get(url, stream=True, timeout=45, allow_redirects=True)
        ct = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        cd = r.headers.get("Content-Disposition") or ""
        cl = r.headers.get("Content-Length")
        if ct in SAFE_TEXT_CT:
            r.close(); print(f"[SKIP] safe-text content-type: {ct} {url}")
            return None
        if cl and cl.isdigit() and int(cl) > max_bytes:
            r.close(); print(f"[SKIP] too large: {cl} > {max_bytes} {url}")
            return None

        # 파일명 결정을 위한 힌트
        disp_name = None
        m = re.search(r"filename\\*=UTF-8''([^;\\r\\n]+)", cd)
        if m:
            disp_name = m.group(1)
        else:
            m2 = re.search(r'filename="?([^";]+)"?', cd)
            if m2:
                disp_name = m2.group(1)
        # URL basename fallback
        from urllib.parse import unquote
        base_from_url = os.path.basename(pu.path.rstrip("/")) or "file.bin"
        fname = sanitize_filename(disp_name or unquote(base_from_url), fallback="file.bin")

        # 최종 경로 (.quarantine 확장자 강제)
        sha = hashlib.sha256()
        tmp_path = dst_dir / (fname + ".part")
        final_name = fname + ".quarantine"
        final_path = dst_dir / final_name

        downloaded = 0
        with open(tmp_path, "wb") as f:
            for chunk in r.iter_content(8192):
                if not chunk:
                    continue
                downloaded += len(chunk)
                if downloaded > max_bytes:
                    r.close(); f.close()
                    tmp_path.unlink(missing_ok=True)
                    print(f"[SKIP] exceeded {max_bytes} bytes: {url}")
                    return None
                sha.update(chunk)
                f.write(chunk)
        r.close()
        os.replace(tmp_path, final_path)

        return {
            "url": url,
            "saved_as": str(final_path),
            "sha256": sha.hexdigest(),
            "size": downloaded,
            "content_type": ct,
        }
    except Exception as e:
        print(f"[WARN] download failed: {url} ({e})", file=sys.stderr)
        return None


def zip_quarantine_folder(folder: Path, zip_path: Path, *, password: Optional[str] = None):
    folder = Path(folder)
    zip_path = Path(zip_path)
    zip_path.parent.mkdir(parents=True, exist_ok=True)

    if password and pyminizip is not None:
        # pyminizip은 파일 단위라 재귀적으로 수집
        files = []
        for root, _, fns in os.walk(folder):
            for fn in fns:
                files.append(os.path.join(root, fn))
        # 단일 zip 생성: pyminizip.compress_multiple( ... )
        try:
            pyminizip.compress_multiple(files, [str(folder)] * len(files), str(zip_path), password, 5)
            return True
        except Exception as e:
            print(f"[WARN] pyminizip failed, fallback to plain zip: {e}", file=sys.stderr)

    # 일반 zip (암호 없음)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(folder):
            for fn in files:
                full = Path(root) / fn
                rel = full.relative_to(folder)
                zf.write(full, arcname=str(rel))
    return True
