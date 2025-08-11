"""
통합 포럼 크롤러
- crawler_min.py 코드(requests+BeautifulSoup 기반)의 안전장치/셀렉터/YAML/저장(JSONL, SQLite) 유지
- 기존존 코드(Playwright 기반)를 엔진으로 추가해 JS 의존 사이트 대응
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

    # playwright는 context manager로 수명 관리
    # 포럼 중 하나라도 playwright가 필요하면 한 번 열고 재사용
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


if __name__ == "__main__":
    if BeautifulSoup is None:
        print("[ERR] beautifulsoup4가 설치되어 있지 않습니다. `pip install beautifulsoup4 lxml`", file=sys.stderr)
        sys.exit(2)
    main()
