# -*- coding: utf-8 -*-
"""
DarkForums 후처리 (이중 파이프라인: Strict + Relaxed + Triage)
- 입력: 기본은 crawler\out 최신 .jsonl 자동 탐색 (또는 --input)
- Strict 파이프라인: 보수적 필터 → 코어 데이터셋
- Relaxed 파이프라인: 완화 필터 → Triage(Keep/Review/Drop) 분리
- 공통: 정규화, 필터링(공지/액션URL/CF차단), 중복 제거, 키워드 태깅, 텔레그램/가격 추출
- 출력:
  - *.strict.filtered.jsonl + *.strict.summary.csv
  - *.relaxed.filtered.jsonl + *.relaxed.summary.csv
  - *.relaxed.keep.jsonl / *.relaxed.review.jsonl / *.relaxed.drop_meta.jsonl
"""

import argparse, csv, json, re, sys, hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple, List

# -------------------- 규칙 --------------------
CF_TITLES = {
    "Checking your browser before accessing darkforums.st",
    "Just a moment...",
}

# Strict 제외 URL 패턴
STRICT_EXCLUDE_URL_PATTERNS = [
    r"/Announcement-",                               # 공지
    r"/Forum-[A-Za-z0-9\-]+$",                       # 포럼 루트
    r"[?&](action)=(newpost|lastpost)$",             # 액션 링크만
    r"[?&](page)=\d+$",                              # 페이지 파라미터
]

# Relaxed 제외 URL 패턴 (page 허용)
RELAXED_EXCLUDE_URL_PATTERNS = [
    r"/Announcement-",
    r"/Forum-[A-Za-z0-9\-]+$",
    r"[?&](action)=(newpost|lastpost)$",
]

KEYWORDS = {
    "db_leak":  [r"\b(leak|database|dump|combo|breach|exfil)\b"],
    "gov_id":   [r"\b(ssn|sin|aadhaar|passport|cnic|national ?id)\b"],
    "gaming":   [r"\b(steam|pubg|gta|freefire|ubisoft|game)\b"],
    "source":   [r"\b(source code|sdk|repo|src)\b"],
    "account":  [r"\b(account|credentials|login|user:pass|mail access)\b"],
}

RE_TELEGRAM = re.compile(r"(?<!\w)@([A-Za-z0-9_]{4,})")
RE_PRICE    = re.compile(r"(?:(USD|\$|IDR)\s*)?(\d{1,3}(?:[.,]\d{3})*|\d+)\s*(?:USD|\$|IDR)?", re.I)

# -------------------- 유틸 --------------------
def latest_jsonl(in_dir: Path) -> Optional[Path]:
    files = sorted(in_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None

def hash_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", "ignore")).hexdigest()

def normalize_title(title: Optional[str], url: str) -> str:
    if title and title.strip() and title not in CF_TITLES:
        return title.strip()
    slug = url.rstrip("/").split("/")[-1].split("?")[0]
    return slug.replace("-", " ").strip() or url

def is_cloudflare_block(title: Optional[str], posts, keep_cf: bool) -> bool:
    if keep_cf:
        return False
    if title in CF_TITLES:
        return True
    if isinstance(posts, list) and len(posts) == 0:
        return True
    return False

def tag_keywords(text: str) -> List[str]:
    if not text:
        return []
    t = text.lower()
    out = []
    for k, pats in KEYWORDS.items():
        if any(re.search(p, t) for p in pats):
            out.append(k)
    return sorted(set(out))

def extract_contacts_and_prices(text: str) -> Tuple[List[str], List[dict]]:
    if not text:
        return [], []
    tg = ["@" + m for m in RE_TELEGRAM.findall(text)]
    prices = []
    for cur, val in RE_PRICE.findall(text):
        v = val.replace(",", "").replace(".", "")
        try:
            n = int(v)
        except:
            continue
        prices.append({"currency": (cur.upper() if cur else None), "value": n})
    return sorted(set(tg)), prices

def clean_post(p: dict) -> dict:
    c = (p.get("content") or "").strip()
    c = re.sub(r"\[/?font[^\]]*\]", "", c, flags=re.I)
    c = re.sub(r"\s+", " ", c).strip()
    p["content"] = c or None
    p["author"] = (p.get("author") or None)
    ts = p.get("posted_at")
    if isinstance(ts, str) and ts:
        try:
            datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except:
            p["posted_at"] = datetime.now(timezone.utc).isoformat()
    return p

def canon_thread_url(u: str) -> str:
    # page=n 제거(중복 완화), 필요시 Relaxed에서 원본도 함께 보관
    return re.sub(r"([?&]page=\d+)$", "", u or "", flags=re.I)

# -------------------- 공통 처리 --------------------
def filter_records(
    input_path: Path,
    exclude_url_patterns: List[str],
    keep_cf: bool,
    min_chars: int,
    allow_empty: bool
):
    EXCLUDE_URL_RE = re.compile("|".join(exclude_url_patterns), re.I)

    seen_threads, seen_posts = set(), set()
    total, kept = 0, 0
    out_records = []
    csv_rows = []

    with input_path.open("r", encoding="utf-8") as fin:
        for line in fin:
            if not line.strip():
                continue
            total += 1
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            url_raw = obj.get("thread_url") or ""
            url = canon_thread_url(url_raw)
            title = normalize_title(obj.get("title"), url_raw)
            posts = obj.get("posts") or []

            if EXCLUDE_URL_RE.search(url_raw):
                continue
            if is_cloudflare_block(obj.get("title"), posts, keep_cf=keep_cf):
                continue

            norm_posts = []
            for p in posts:
                p = clean_post(p)
                if min_chars > 0:
                    if not p.get("content") or len(p["content"]) < min_chars:
                        continue
                phash = hash_text((p.get("post_url") or "") + "|" + (p.get("content") or ""))
                if phash in seen_posts:
                    continue
                seen_posts.add(phash)
                norm_posts.append(p)

            if not norm_posts and not allow_empty:
                continue

            thread_key = obj.get("thread_hash") or hash_text(url)
            if thread_key in seen_threads:
                continue
            seen_threads.add(thread_key)

            sample = " ".join([(norm_posts[0].get("content") or "")] +
                              [(q.get("content") or "") for q in norm_posts[1:3]]) if norm_posts else title
            tags = tag_keywords(title + " " + sample)
            tg_handles, prices = extract_contacts_and_prices(title + " " + sample)

            rec = {
                "source"     : obj.get("source") or "darkforums",
                "thread_url" : url_raw,
                "canon_url"  : url,
                "title"      : title,
                "thread_hash": thread_key,
                "fetched_at" : obj.get("fetched_at"),
                "post_count" : len(norm_posts),
                "posts"      : norm_posts,
                "tags"       : tags,
                "contacts"   : {"telegram": tg_handles} if tg_handles else {},
                "prices"     : prices,
            }
            out_records.append(rec); kept += 1

            snippet = ""
            if norm_posts:
                snippet = (norm_posts[0].get("content") or "")[:160].replace("\n", " ")
            csv_rows.append([
                rec["canon_url"] or rec["thread_url"],
                rec["title"],
                rec["post_count"],
                "|".join(tags),
                ",".join(tg_handles),
                snippet
            ])

    return out_records, csv_rows, total, kept

def write_jsonl(path: Path, records: List[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

def write_csv(path: Path, rows: List[list]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fcsv:
        w = csv.writer(fcsv)
        w.writerow(["url","title","post_count","tags","telegram","first_snippet"])
        w.writerows(rows)

# -------------------- Triage (Relaxed 결과에 적용) --------------------
def triage_score(rec: dict) -> int:
    score = 0
    title = rec.get("title") or ""
    tags = rec.get("tags") or []
    post_count = rec.get("post_count") or 0
    first_len = 0
    if rec.get("posts"):
        first_len = len((rec["posts"][0].get("content") or ""))

    # + Signals
    if any(t in tags for t in ["db_leak","source","account"]): score += 2
    if rec.get("contacts", {}).get("telegram"): score += 1
    if first_len >= 80: score += 1
    if post_count >= 3: score += 1

    # - Signals
    if title in CF_TITLES: score -= 2
    if re.search(r"[?&](action)=(newpost|lastpost)", rec.get("thread_url",""), re.I): score -= 1
    # 캐논 URL과 원본 URL이 다르고, 원본이 page=만 다른 경우는 페널티 완화(중복성)
    # 필요시 추가 규칙 가능

    return score

def triage_split(records: List[dict], keep_th:int=2, drop_th:int=0):
    keep, review, drop_meta = [], [], []
    for r in records:
        s = triage_score(r)
        if s >= keep_th:
            keep.append(r)
        elif s < drop_th:  # drop_th 미만
            # 메타만 보관(본문 제거)
            meta = dict(r)
            meta["posts"] = []
            drop_meta.append(meta)
        else:
            review.append(r)
    return keep, review, drop_meta

# -------------------- entrypoint --------------------
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--input",  help="지정 시 해당 .jsonl 사용")
    ap.add_argument("--out-dir", default=None, help="out 폴더 강제 지정(기본: crawler\\out)")
    ap.add_argument("--pipeline", choices=["strict","relaxed","dual"], default="dual",
                    help="어떤 파이프라인을 실행할지 (기본 dual)")
    # Strict 파라미터
    ap.add_argument("--min-chars-strict", type=int, default=20)
    ap.add_argument("--keep-cf-strict", action="store_true", help="Strict에서도 CF 페이지 유지")
    # Relaxed 파라미터
    ap.add_argument("--min-chars-relaxed", type=int, default=5)
    ap.add_argument("--keep-cf-relaxed", action="store_true", default=True)  # 기본 유지
    ap.add_argument("--allow-empty-relaxed", action="store_true", default=True)  # 기본 허용
    # Triage 파라미터
    ap.add_argument("--keep-threshold", type=int, default=2)
    ap.add_argument("--drop-threshold", type=int, default=0)

    args = ap.parse_args()

    here = Path(__file__).resolve().parent
    default_out_dir = Path(args.out_dir) if args.out_dir else (here.parents[2] / "out")
    print(f"- 기본 out: {default_out_dir} (최신 .jsonl 자동 탐색)", file=sys.stderr)

    # 입력 파일 결정
    if args.input:
        in_path = Path(args.input)
        if not in_path.is_file():
            print(f"[ERR] 입력 파일이 존재하지 않습니다: {in_path}", file=sys.stderr); sys.exit(2)
    else:
        in_path = latest_jsonl(default_out_dir)
        if not in_path:
            print(f"[ERR] JSONL 파일을 찾지 못했습니다: {default_out_dir}", file=sys.stderr); sys.exit(2)

    stem = in_path.stem
    base_dir = in_path.parent

    # ---------------- Strict ----------------
    if args.pipeline in ("strict","dual"):
        strict_ex = STRICT_EXCLUDE_URL_PATTERNS
        strict_records, strict_rows, total_s, kept_s = filter_records(
            in_path,
            exclude_url_patterns=strict_ex,
            keep_cf=args.keep_cf_strict,
            min_chars=args.min_chars_strict,
            allow_empty=False,
        )
        out_jsonl_strict = base_dir / f"{stem}.strict.filtered.jsonl"
        out_csv_strict   = base_dir / f"{stem}.strict.summary.csv"
        write_jsonl(out_jsonl_strict, strict_records)
        write_csv(out_csv_strict, strict_rows)
        print(f"[STRICT] input={in_path.name}, total={total_s}, kept={kept_s}, out={out_jsonl_strict.name}", file=sys.stderr)

    # ---------------- Relaxed + Triage ----------------
    if args.pipeline in ("relaxed","dual"):
        relaxed_ex = RELAXED_EXCLUDE_URL_PATTERNS
        relaxed_records, relaxed_rows, total_r, kept_r = filter_records(
            in_path,
            exclude_url_patterns=relaxed_ex,
            keep_cf=args.keep_cf_relaxed,
            min_chars=args.min_chars_relaxed,
            allow_empty=args.allow_empty_relaxed,
        )
        out_jsonl_relaxed = base_dir / f"{stem}.relaxed.filtered.jsonl"
        out_csv_relaxed   = base_dir / f"{stem}.relaxed.summary.csv"
        write_jsonl(out_jsonl_relaxed, relaxed_records)
        write_csv(out_csv_relaxed, relaxed_rows)
        print(f"[RELAXED] input={in_path.name}, total={total_r}, kept={kept_r}, out={out_jsonl_relaxed.name}", file=sys.stderr)

        # Triage split
        keep, review, drop_meta = triage_split(
            relaxed_records,
            keep_th=args.keep_threshold,
            drop_th=args.drop_threshold
        )
        write_jsonl(base_dir / f"{stem}.relaxed.keep.jsonl", keep)
        write_jsonl(base_dir / f"{stem}.relaxed.review.jsonl", review)
        write_jsonl(base_dir / f"{stem}.relaxed.drop_meta.jsonl", drop_meta)
        print(f"[TRIAGE] keep={len(keep)}, review={len(review)}, drop_meta={len(drop_meta)}", file=sys.stderr)
