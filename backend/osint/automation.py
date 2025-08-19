#!/usr/bin/env python3
"""
OMT OSINT Automation - Single file version
- Crawled forum data -> normalize -> IOC extraction -> score -> JSONL export
- Supports input formats: .json, .jsonl, .csv, .html, .txt
- No external dependencies required
"""

import os, re, csv, json, hashlib, argparse, datetime, pathlib
from html.parser import HTMLParser


# -----------------------
# HTML parser (내장만 사용)
# -----------------------
class SimpleHTMLStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.texts = []

    def handle_data(self, d):
        self.texts.append(d)

    def get_text(self):
        return " ".join(self.texts)


def strip_html(path: str) -> str:
    with open(path, "r", errors="ignore") as f:
        content = f.read()
    s = SimpleHTMLStripper()
    s.feed(content)
    return " ".join(s.get_text().split())


# -----------------------
# Utils
# -----------------------
def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def guess_timestamp(value) -> str:
    if not value:
        return ""
    if isinstance(value, (int, float)):
        try:
            return datetime.datetime.utcfromtimestamp(float(value)).isoformat() + "Z"
        except Exception:
            return ""
    if isinstance(value, str):
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d",
            "%d-%m-%Y %H:%M",
            "%Y/%m/%d %H:%M",
            "%Y.%m.%d %H:%M",
        ):
            try:
                return datetime.datetime.strptime(value, fmt).isoformat() + "Z"
            except Exception:
                continue
        if "T" in value or "-" in value or ":" in value:
            return value
    return ""


# -----------------------
# Input walker
# -----------------------
def walk_inputs(input_dir: str):
    exts = {".json", ".jsonl", ".csv", ".html", ".htm", ".txt"}
    for root, _, files in os.walk(input_dir):
        for name in files:
            ext = pathlib.Path(name).suffix.lower()
            if ext not in exts:
                continue
            path = os.path.join(root, name)
            if ext in {".html", ".htm"}:
                yield {"_source_path": path, "text": strip_html(path)}
            elif ext == ".txt":
                with open(path, "r", errors="ignore") as f:
                    yield {"_source_path": path, "text": " ".join(f.read().split())}
            elif ext in {".json", ".jsonl"}:
                with open(path, "r", errors="ignore") as f:
                    first = f.read(1)
                    f.seek(0)
                    if first == "[":
                        try:
                            data = json.load(f)
                            for row in data:
                                row["_source_path"] = path
                                yield row
                        except Exception:
                            continue
                    else:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                row = json.loads(line)
                                row["_source_path"] = path
                                yield row
                            except Exception:
                                continue
            elif ext == ".csv":
                with open(path, "r", errors="ignore", newline="") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        row["_source_path"] = path
                        yield row


# -----------------------
# Normalization
# -----------------------
FIELD_MAP = {
    "url": ["url", "link", "source_url"],
    "forum": ["forum", "site", "board"],
    "thread_id": ["thread_id", "threadId", "id", "post_id"],
    "author": ["author", "user", "username", "nick"],
    "title": ["title", "subject", "headline"],
    "text": ["text", "content", "body"],
    "posted_at": ["posted_at", "created_at", "published", "date", "time"],
    "lang": ["lang", "language"],
}


def _get_first(d, keys):
    for k in keys:
        if k in d and d[k]:
            return d[k]
    return ""


def normalize_record(raw):
    rec = {}
    for k, aliases in FIELD_MAP.items():
        rec[k] = _get_first(raw, aliases)
    if not rec.get("text") and "html" in raw:
        rec["text"] = raw.get("html", "")
    rec["content_hash"] = content_hash(rec.get("title", "") + "\n" + rec.get("text", ""))
    rec["posted_at"] = guess_timestamp(rec.get("posted_at"))
    rec["source_path"] = raw.get("_source_path", "")
    rec["_raw"] = {k: v for k, v in raw.items() if not k.startswith("_")}
    return rec


# -----------------------
# IOC extraction
# -----------------------
PATTERNS = {
    "ipv4": re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?!$)|$)){4}\b"),
    "email": re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[A-Za-z]{2,}\b"),
    "url": re.compile(r"\b(?:https?://|www\.)[\w\-.~/#%?=&+,:;!()]+", re.IGNORECASE),
    "btc": re.compile(r"\b(?:bc1|[13])[a-zA-HJ-NP-Z0-9]{25,39}\b"),
    "eth": re.compile(r"\b0x[a-fA-F0-9]{40}\b"),
    "sha256": re.compile(r"\b[a-fA-F0-9]{64}\b"),
    "cve": re.compile(r"\bCVE-\d{4}-\d{4,7}\b", re.IGNORECASE),
    "telegram": re.compile(r"(?:t\.me/(?:joinchat/)?|@)[A-Za-z0-9_]{5,}"),
    "discord": re.compile(r"discord(?:\.gg|app\.com/invite)/[\w-]+", re.IGNORECASE),
}


def extract_iocs(text):
    iocs = {}
    for name, pat in PATTERNS.items():
        found = list({m.group(0) for m in pat.finditer(text)})
        if found:
            iocs[name] = found
    return iocs


KEYWORDS = [
    "sell",
    "for sale",
    "leak",
    "database",
    "db",
    "dump",
    "access",
    "ransomware",
    "botnet",
    "checker",
    "logs",
    "stealer",
    "infostealer",
    "combo",
    "dox",
    "exploit",
    "0day",
]


def keyword_hits(text):
    hits = []
    lower = text.lower() if text else ""
    for kw in KEYWORDS:
        if kw in lower:
            hits.append(kw)
    return sorted(set(hits))


# -----------------------
# Scoring
# -----------------------
def compute_score(text, iocs):
    hits = keyword_hits(text)
    s = 0
    s += 3 * len(iocs.get("cve", []))
    s += 2 * len(iocs.get("ipv4", [])) + 2 * len(iocs.get("email", []))
    s += 1 * sum(len(v) for k, v in iocs.items() if k not in {"cve", "ipv4", "email"})
    s += 2 * len(hits)
    s += 1 if text and len(text) > 60 else 0
    return {"score": min(100, s), "keyword_hits": hits}


# -----------------------
# Export
# -----------------------
def write_jsonl(records, out_path):
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# -----------------------
# Pipeline
# -----------------------
def pipeline(input_dir, out_jsonl):
    seen = set()
    results = []
    for raw in walk_inputs(input_dir):
        rec = normalize_record(raw)
        text = rec.get("title", "") + "\n" + rec.get("text", "")
        iocs = extract_iocs(text)
        rec.update({"iocs": iocs, **compute_score(text, iocs)})
        h = rec.get("content_hash")
        if h in seen:
            continue
        seen.add(h)
        results.append(rec)
    write_jsonl(results, out_jsonl)
    return len(results)


# -----------------------
# CLI
# -----------------------
def main():
    ap = argparse.ArgumentParser(
        description="OMT OSINT automation: normalize + IOC extract + JSONL export"
    )
    ap.add_argument(
        "-i", "--input", required=True, help="Input directory with crawled files"
    )
    ap.add_argument("-o", "--out", required=True, help="Output JSONL path")
    args = ap.parse_args()
    n = pipeline(args.input, args.out)
    print(f"[+] Wrote {n} records to {args.out}")

# 내부 호출용 엔트리포인트 (Flask 등에서 사용)
def run_osint(input_dir: str, out_path: str):
    """
    입력 폴더의 파일들을 정규화 → IOC 추출 → 스코어링 → 중복제거 후
    JSONL(out_path)로 저장합니다.
    반환 예: {"count": N, "out": "./data/osint_out.jsonl"}
    """
    n = pipeline(input_dir, out_path)
    return {"count": n, "out": out_path}


if __name__ == "__main__":
    main()