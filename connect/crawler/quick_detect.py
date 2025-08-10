# quick_detect.py
import json, re, argparse, datetime, pathlib, collections

RULE = {
  "kw_high": ["for sale","db leak","initial access","full dump","ransomware"],
  "kw_med":  ["vpn","combo list","cve-","rce"],
  "stop":    ["giveaway","test post","looking for team"],
  "re": {
    "email": r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
    "ipv4":  r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b",
    "domain":r"\b[a-z0-9-]+(?:\.[a-z0-9-]+){1,}\b",
    "btc":   r"\b(bc1|[13])[a-zA-HJ-NP-Z0-9]{25,39}\b"
  },
  "w": {"high":2,"med":1,"ioc":2,"neg":-2},
  "th": {"alert":5,"review":3}
}

def score_text(txt: str):
  t = txt.lower()
  if any(s in t for s in RULE["stop"]):
    return 0, {}
  s = 0
  s += sum(k in t for k in RULE["kw_high"]) * RULE["w"]["high"]
  s += sum(k in t for k in RULE["kw_med"])  * RULE["w"]["med"]
  iocs = {k: sorted(set(re.findall(rgx, t, re.I))) for k, rgx in RULE["re"].items()}
  iocs = {k:v for k,v in iocs.items() if v}
  if iocs:
    s += RULE["w"]["ioc"]
  return s, {"iocs": iocs}

def run_file(in_path: pathlib.Path, out_path: pathlib.Path):
  n_threads = n_posts = n_alert = n_review = 0
  domains = collections.Counter()
  out_path.parent.mkdir(parents=True, exist_ok=True)

  with in_path.open(encoding="utf-8") as f, out_path.open("w", encoding="utf-8") as g:
    for line in f:
      if not line.strip():
        continue
      t = json.loads(line)
      n_threads += 1
      title = t.get("title") or ""
      bodies = " ".join(p.get("content","") for p in t.get("posts", []))
      txt = f"{title}\n{bodies}"
      n_posts += len(t.get("posts", []))

      sc, meta = score_text(txt)
      level = "none"
      if sc >= RULE["th"]["alert"]:
        level = "alert"; n_alert += 1
      elif sc >= RULE["th"]["review"]:
        level = "review"; n_review += 1

      url = t.get("thread_url") or ""
      if "://" in url:
        dom = url.split("://",1)[1].split("/",1)[0]
        domains[dom] += 1

      g.write(json.dumps({
        "level": level, "score": sc, "title": t.get("title"),
        "thread_url": url, "matched": meta, "source": t.get("source"),
        "evaluated_at": datetime.datetime.utcnow().isoformat()+"Z"
      }, ensure_ascii=False) + "\n")

  print(f"threads={n_threads}, posts={n_posts}, alert={n_alert}, review={n_review}")
  print("top domains:", domains.most_common(5))

# 맨 아래
import pathlib, glob

def resolve_in_path(s):
    p = pathlib.Path(s)
    if s.lower() == "latest":
        cands = sorted(pathlib.Path("out").glob("crawl_*.jsonl"))
        if not cands: raise FileNotFoundError("no crawl_*.jsonl under ./out")
        return cands[-1]
    if p.exists(): return p
    alt = pathlib.Path(__file__).resolve().parents[2] / p  # 워크스페이스 루트 추정
    if alt.exists(): return alt
    raise FileNotFoundError(s)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--in",  dest="in_path",  default="latest", help="crawler JSONL path or 'latest'")
    ap.add_argument("--out", dest="out_path", default="out/alerts.jsonl", help="alerts JSONL path")
    a = ap.parse_args()
    run_file(resolve_in_path(a.in_path), pathlib.Path(a.out_path))
