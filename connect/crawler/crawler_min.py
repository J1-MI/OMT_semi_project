# crawler_min.py
# pip install "requests[socks]" beautifulsoup4 pyyaml lxml

import argparse, json, os, re, sys, time, hashlib, random
from datetime import datetime, timezone
from urllib.parse import urljoin
from pathlib import Path

import requests
from bs4 import BeautifulSoup
import yaml
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    import sqlite3
except Exception:
    sqlite3 = None

# -------------------- CONST & SAFE GUARDS --------------------
UA = {"User-Agent": "Mozilla/5.0 (compatible; DarkWatch/1.0)"}
HTML_CT_ALLOWED = ("text/html", "application/xhtml+xml")
ATTACHMENT_URL_PATTERN = re.compile(
    r"(?i)(/attachment|/attachments|/upload|/uploads|/files|/download)"
    r"|(\.(zip|7z|rar|exe|iso|apk|jar|bat|ps1|dll|scr|docm|xlsm|pdf|gz|bz2|xz)(?:$|\?))"
)

def is_attachment_url(url: str) -> bool:
    return bool(ATTACHMENT_URL_PATTERN.search(url or ""))

def make_session(proxies=None) -> requests.Session:
    s = requests.Session()
    s.headers.update(UA)
    retry = Retry(
        total=3, backoff_factor=1.2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"]
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    if proxies:
        s.proxies.update(proxies)
    return s

def safe_get_html(url: str, session: requests.Session, timeout=30, max_bytes=3_000_000) -> str:
    if is_attachment_url(url):
        raise ValueError(f"Blocked attachment-like URL: {url}")
    current = url
    for _ in range(5):  # redirect depth
        r = session.get(current, timeout=timeout, allow_redirects=False, stream=True)
        try:
            if 300 <= r.status_code < 400 and "Location" in r.headers:
                nxt = urljoin(current, r.headers["Location"])
                if is_attachment_url(nxt):
                    raise ValueError(f"Redirected to blocked URL: {nxt}")
                current = nxt
                r.close()
                continue

            cd = (r.headers.get("Content-Disposition") or "").lower()
            if "attachment" in cd:
                raise ValueError("Server suggests attachment download.")
            ct = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ct not in HTML_CT_ALLOWED:
                raise ValueError(f"Non-HTML Content-Type: {ct or 'unknown'}")

            cl = r.headers.get("Content-Length")
            if cl and cl.isdigit() and int(cl) > max_bytes:
                raise ValueError(f"Response too large: {cl} bytes")

            chunks, total = [], 0
            for chunk in r.iter_content(8192):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(f"Exceeded {max_bytes} bytes")
                chunks.append(chunk)
            raw = b"".join(chunks)
            enc = r.encoding or "utf-8"
            try:
                return raw.decode(enc, errors="replace")
            except LookupError:
                return raw.decode("utf-8", errors="replace")
        finally:
            r.close()
    raise RuntimeError("Too many redirects")

def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()

def pick_one(soup_or_node, candidates, many=False):
    """candidates: str or [str]. many=True면 list 반환"""
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

# -------------------- PARSERS --------------------
def parse_thread(thread_url: str, sel: dict, session) -> dict:
    html = safe_get_html(thread_url, session=session)
    s = BeautifulSoup(html, "lxml")

    title_el, _ = pick_one(s, sel["thread_title"])
    thread_title = title_el.get_text(" ", strip=True) if title_el else None

    nodes, _ = pick_one(s, sel["post_container"], many=True)
    posts = []
    for node in nodes:
        content_el, _ = pick_one(node, sel["content"])
        content = content_el.get_text(" ", strip=True) if content_el else ""

        author_el, _ = pick_one(node, sel["author"])
        author = author_el.get_text(strip=True) if author_el else None

        time_el, _ = pick_one(node, sel["posted_time"])
        if time_el and time_el.has_attr("datetime"):
            posted_at = time_el["datetime"]
        else:
            posted_at = time_el.get_text(" ", strip=True) if time_el else utcnow_iso()

        pl_el, _ = pick_one(node, sel.get("post_permalink"))
        post_url = urljoin(thread_url, pl_el.get("href")) if pl_el and pl_el.get("href") else thread_url

        # attachments metadata only (never fetch)
        attachments = []
        att_nodes, _ = pick_one(node, sel.get("attachment_block"), many=True)
        for a in att_nodes:
            name_el, _ = pick_one(a, sel.get("attachment_name"))
            size_el, _ = pick_one(a, sel.get("attachment_size"))
            alink = a.find("a")
            att_url = urljoin(thread_url, alink.get("href")) if (alink and alink.get("href")) else None
            if att_url:
                attachments.append({
                    "display_filename": name_el.get_text(strip=True) if name_el else None,
                    "display_size": size_el.get_text(strip=True) if size_el else None,
                    "attachment_url": att_url
                })

        posts.append({
            "post_url": post_url,
            "author": author,
            "posted_at": posted_at,
            "content": content,
            "attachments": attachments
        })

    return {
        "thread_url": thread_url,
        "title": thread_title,
        "posts": posts,
        "fetched_at": utcnow_iso(),
        "thread_hash": sha256((thread_title or "") + "".join(p["content"] for p in posts)[:5000])
    }

def crawl_forum(key: str, conf: dict, session, max_pages=2, sleep_sec=(1.0, 2.0)):
    sel = conf[key]
    visited_threads = set()
    results = []

    for start_url in sel["list_urls"]:
        next_url, page = start_url, 1
        while next_url and page <= max_pages:
            html = safe_get_html(next_url, session=session)
            s = BeautifulSoup(html, "lxml")

            # collect thread links (support multiple candidates)
            link_nodes = []
            for cand in (sel["thread_link"] if isinstance(sel["thread_link"], list) else [sel["thread_link"]]):
                try:
                    link_nodes = s.select(cand)
                except Exception:
                    link_nodes = []
                if link_nodes:
                    break

            # de-dup by final href
            seen_on_page = set()
            for a in link_nodes:
                href = a.get("href")
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
                    data = parse_thread(thread_url, sel, session)
                    results.append({"source": key, **data})
                except Exception as e:
                    print(f"[WARN] parse_thread failed: {thread_url} ({e})", file=sys.stderr)

                time.sleep(random.uniform(*sleep_sec))

            np_el, _ = pick_one(s, sel.get("next_page"))
            next_url = urljoin(next_url, np_el.get("href")) if np_el and np_el.get("href") else None
            page += 1
            time.sleep(random.uniform(*sleep_sec))
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
        print("[SKIP] sqlite3 unavailable.", file=sys.stderr); return
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS posts (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      source TEXT, thread_url TEXT, post_url TEXT,
      title TEXT, author TEXT, posted_at TEXT,
      content TEXT, fetched_at TEXT
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS attachments (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      post_id INTEGER,
      display_filename TEXT, display_size TEXT, attachment_url TEXT,
      FOREIGN KEY(post_id) REFERENCES posts(id)
    )""")
    for rec in records:
        source = rec["source"]
        thread_url = rec["thread_url"]
        title = rec.get("title")
        fetched_at = rec.get("fetched_at")
        for p in rec["posts"]:
            cur.execute("""
            INSERT INTO posts(source,thread_url,post_url,title,author,posted_at,content,fetched_at)
            VALUES(?,?,?,?,?,?,?,?)""",
            (source, thread_url, p["post_url"], title, p["author"], p["posted_at"], p["content"], fetched_at))
            pid = cur.lastrowid
            for a in p.get("attachments", []):
                cur.execute("""
                INSERT INTO attachments(post_id,display_filename,display_size,attachment_url)
                VALUES(?,?,?,?)""", (pid, a["display_filename"], a["display_size"], a["attachment_url"]))
    conn.commit(); conn.close()
    print(f"[OK] SQLite saved: {path}")

# -------------------- MAIN --------------------
def load_config_smart(path_str: str) -> dict:
    """현재 작업폴더 → 스크립트 폴더 순서로 탐색"""
    cand = Path(path_str)
    if cand.exists():
        return yaml.safe_load(cand.read_text(encoding="utf-8"))
    here = Path(__file__).parent / path_str
    if here.exists():
        return yaml.safe_load(here.read_text(encoding="utf-8"))
    raise FileNotFoundError(f"selectors not found: {path_str}")

def main():
    ap = argparse.ArgumentParser(description="Minimal forum crawler (HTML-only, no attachments)")
    ap.add_argument("--config", default="selectors.yaml")
    ap.add_argument("--forums", nargs="+", default=["darkforums"])
    ap.add_argument("--pages", type=int, default=2, help="pages per list")
    ap.add_argument("--tor", action="store_true", help="use Tor SOCKS proxy (127.0.0.1:9150)")
    ap.add_argument("--out-jsonl", default="out/crawl.jsonl")
    ap.add_argument("--out-sqlite", default=None, help="optional SQLite path")
    args = ap.parse_args()

    try:
        conf = load_config_smart(args.config)
    except Exception as e:
        print(f"[ERR] load config failed: {e}", file=sys.stderr)
        sys.exit(2)

    proxies = None
    if args.tor:
        proxies = {"http": "socks5h://127.0.0.1:9150", "https": "socks5h://127.0.0.1:9150"}
        print("[INFO] Tor proxy enabled via socks5h://127.0.0.1:9150")

    session = make_session(proxies=proxies)

    all_records = []
    for key in args.forums:
        if key not in conf:
            print(f"[WARN] forum key not in config: {key}", file=sys.stderr)
            continue
        print(f"[INFO] Crawling: {key}")
        try:
            recs = crawl_forum(key, conf, session, max_pages=args.pages)
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
    main()
