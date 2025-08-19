# backend/alert/alert_system/core.py
import os, json, time, uuid, hashlib, re
from datetime import datetime, timedelta
from collections import Counter  # (남겨둠: 필요 시 확장)
from .github_notifier import create_github_issue  # 같은 폴더

BASE_DIR = os.path.dirname(__file__)
RAW_DATA_JSONL_FILES = [
    os.path.join(BASE_DIR, 'darkforums0250817_181707.jsonl')  # 샘플
]
PROCESSED_IDS_LOG_FILE = os.path.join(BASE_DIR, 'processed_ids.log')
MEMOS_FILE = os.path.join(BASE_DIR, 'analysis_memos.json')

# --- 메모 로딩 ---
def load_memos():
    if not os.path.exists(MEMOS_FILE):
        return {}
    try:
        with open(MEMOS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (IOError, json.JSONDecodeError):
        return {}
memos = load_memos()

# --- 중복 처리 방지 ---
def get_processed_ids():
    if not os.path.exists(PROCESSED_IDS_LOG_FILE):
        return set()
    with open(PROCESSED_IDS_LOG_FILE, 'r', encoding='utf-8') as f:
        return set(line.strip() for line in f)

def mark_id_as_processed(item_id: str):
    with open(PROCESSED_IDS_LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(str(item_id) + '\n')

# --- 시간 파서 ---
def parse_posted_at(posted_at_str: str, fetched_at_str: str):
    posted_at_lower = (posted_at_str or "").lower()
    try:
        fetched_dt = datetime.fromisoformat((fetched_at_str or "").replace('Z', '+00:00'))
    except (ValueError, TypeError):
        fetched_dt = datetime.now().astimezone()

    if 'yesterday' in posted_at_lower:
        return (fetched_dt - timedelta(days=1)).isoformat()

    ago_match = re.search(r'(\d+)\s+(hour|minute)s?\s+ago', posted_at_lower)
    if ago_match:
        value, unit = int(ago_match.group(1)), ago_match.group(2)
        if unit == 'hour':
            return (fetched_dt - timedelta(hours=value)).isoformat()
        if unit == 'minute':
            return (fetched_dt - timedelta(minutes=value)).isoformat()

    dt_match = re.search(r'(\d{2}-\d{2}-\d{2}),\s+(\d{1,2}:\d{2}\s+[AP]M)', posted_at_str or "")
    if dt_match:
        date_part, time_part = dt_match.groups()
        try:
            return datetime.strptime(f"{date_part} {time_part}", "%d-%m-%y %I:%M %p").isoformat()
        except ValueError:
            pass

    return fetched_dt.isoformat()

# --- 분류 ---
def classify_content(text: str):
    text = (text or "").lower()
    categories = {
        "데이터베이스/정보 유출": ["database", "db", "leak", "email list", "phone list", "ssn", "combo list", "dehashed", "user:pass", "데이터베이스"],
        "기업 기밀/문서": ["document", "confidential", "internal", "company", "military", "기업정보", "내부문서", "기밀문서"],
        "해킹/익스플로잇": ["exploit", "vulnerability", "zeroday", "0day", "poc", "source code", "unpacked", "ddos", "해킹", "취약점"],
        "악성코드/도구": ["rat", "trojan", "stealer", "ransomware", "builder", "cracked", "crypter", "botnet", "악성코드"],
        "부정/비리": ["corruption", "fraud", "embezzlement", "insider", "부정", "비리"],
        "스팸/마케팅": ["marketing", "telegram channel", "telegram", "contact", "스팸", "마케팅"],
    }
    for category, keywords in categories.items():
        if any(k in text for k in keywords):
            return category
    return "일반 토론"

# --- 데이터 적재/정규화 ---
def load_and_analyze_data(file_paths):
    clean_data, raw_threads_for_summary = [], []
    for file_path in file_paths:
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                raw_threads = [json.loads(line) for line in f if line.strip()]
            raw_threads_for_summary.extend(raw_threads)

            for thread in raw_threads:
                thread_title = thread.get('title', 'N/A')
                thread_title_lower = thread_title.lower()
                thread_hash = thread.get('thread_hash')
                for post in thread.get('posts', []):
                    content = post.get('content', '')
                    author = post.get('author', 'unknown')
                    search_text = thread_title_lower + " " + content.lower()

                    leaked_type = classify_content(search_text)
                    risk = "Informational"
                    if leaked_type in ["데이터베이스/정보 유출", "기업 기밀/문서", "해킹/익스플로잇", "악성코드/도구"]:
                        risk = "Critical"
                    elif leaked_type in ["스팸/마케팅", "부정/비리"]:
                        risk = "Warning"

                    post_url = post.get('post_url', str(uuid.uuid4()))
                    item_id = hashlib.sha256(post_url.encode()).hexdigest()
                    iso_ts = parse_posted_at(post.get('posted_at', ''), thread.get('fetched_at', ''))

                    clean_data.append({
                        "id": item_id,
                        "account": author,
                        "host": thread.get('source', 'unknown'),
                        "leaked_data": leaked_type,
                        "risk_level": risk,
                        "date": iso_ts,
                        "raw_content": content,
                        "memo": memos.get(item_id, ""),
                        "thread_hash": thread_hash,
                        "thread_title": thread_title,
                    })
        except FileNotFoundError:
            # 로깅은 호출측에서 처리 가능
            continue
    return clean_data, raw_threads_for_summary

def get_initial_data_payload():
    all_data, raw_threads = load_and_analyze_data(RAW_DATA_JSONL_FILES)
    all_data.sort(key=lambda x: x['date'], reverse=True)
    return {"alerts": all_data, "raw_threads": raw_threads}

# --- SSE 스트리밍 ---
def process_data_and_stream():
    processed_ids = get_processed_ids()
    while True:
        all_clean, _ = load_and_analyze_data(RAW_DATA_JSONL_FILES)
        new_found = False
        for item in all_clean:
            if item['id'] not in processed_ids:
                new_found = True
                try:
                    create_github_issue(item)
                except Exception:
                    pass
                yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                mark_id_as_processed(item['id'])
                processed_ids.add(item['id'])
        if not new_found:
            time.sleep(60)
