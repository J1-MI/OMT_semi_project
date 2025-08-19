
from flask import Flask, render_template, jsonify, Response, request
import json
import os
import time
from datetime import datetime, timedelta
from collections import Counter
import uuid
import hashlib
import re

# github_notifier.py가 같은 폴더에 있음
from github_notifier import create_github_issue

app = Flask(__name__)

#테스트
BASE_DIR = os.path.dirname(__file__)
RAW_DATA_JSONL_FILES = [
    os.path.join(BASE_DIR, 'darkforums0250817_181707.jsonl')
]
PROCESSED_IDS_LOG_FILE = os.path.join(BASE_DIR, 'processed_ids.log')
MEMOS_FILE = os.path.join(BASE_DIR, 'analysis_memos.json')

'''
# --- 설정 ---
RAW_DATA_JSONL_FILES = [
    'darkforums0250817_181707.jsonl' # 테스트용 파일
]
PROCESSED_IDS_LOG_FILE = os.path.join(os.path.dirname(__file__), 'processed_ids.log')
MEMOS_FILE = os.path.join(os.path.dirname(__file__), 'analysis_memos.json')
'''

# --- 메모 로딩 ---
def load_memos():
    if not os.path.exists(MEMOS_FILE): return {}
    try:
        with open(MEMOS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (IOError, json.JSONDecodeError):
        return {}
memos = load_memos()

# --- 중복 처리 방지를 위한 함수 ---
def get_processed_ids():
    if not os.path.exists(PROCESSED_IDS_LOG_FILE): return set()
    with open(PROCESSED_IDS_LOG_FILE, 'r', encoding='utf-8') as f: return set(line.strip() for line in f)

def mark_id_as_processed(item_id):
    with open(PROCESSED_IDS_LOG_FILE, 'a', encoding='utf-8') as f: f.write(str(item_id) + '\n')

# --- 스마트 날짜 분석 함수 ---
def parse_posted_at(posted_at_str, fetched_at_str):
    posted_at_lower = posted_at_str.lower()
    try:
        fetched_dt = datetime.fromisoformat(fetched_at_str.replace('Z', '+00:00'))
    except (ValueError, TypeError):
        fetched_dt = datetime.now().astimezone()

    if 'yesterday' in posted_at_lower:
        return (fetched_dt - timedelta(days=1)).isoformat()
    
    ago_match = re.search(r'(\d+)\s+(hour|minute)s?\s+ago', posted_at_lower)
    if ago_match:
        value, unit = int(ago_match.group(1)), ago_match.group(2)
        if unit == 'hour': return (fetched_dt - timedelta(hours=value)).isoformat()
        if unit == 'minute': return (fetched_dt - timedelta(minutes=value)).isoformat()

    datetime_match = re.search(r'(\d{2}-\d{2}-\d{2}),\s+(\d{1,2}:\d{2}\s+[AP]M)', posted_at_str)
    if datetime_match:
        date_part, time_part = datetime_match.groups()
        try:
            return datetime.strptime(f"{date_part} {time_part}", "%d-%m-%y %I:%M %p").isoformat()
        except ValueError: pass
    
    return fetched_dt.isoformat()

# --- 콘텐츠 기반 상세 분류 로직 ---
def classify_content(text):
    text = text.lower()
    categories = {
        "데이터베이스/정보 유출": ["database", "db", "leak", "email list", "phone list", "ssn", "combo list", "dehashed", "user:pass", "데이터베이스"],
        "기업 기밀/문서": ["document", "confidential", "internal", "company", "military", "기업정보", "내부문서", "기밀문서"],
        "해킹/익스플로잇": ["exploit", "vulnerability", "zeroday", "0day", "poc", "source code", "unpacked", "ddos", "해킹", "취약점"],
        "악성코드/도구": ["rat", "trojan", "stealer", "ransomware", "builder", "cracked", "crypter", "botnet", "악성코드"],
        "부정/비리": ["corruption", "fraud", "embezzlement", "insider", "부정", "비리"],
        "스팸/마케팅": ["marketing", "telegram channel", "telegram", "contact", "스팸", "마케팅"],
    }
    for category, keywords in categories.items():
        if any(keyword in text for keyword in keywords): return category
    return "일반 토론"

# --- 데이터 처리 및 분석 로직 ---
def load_and_analyze_data(file_paths):
    clean_data, raw_threads_for_summary = [], []
    for file_path in file_paths:
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                raw_threads = [json.loads(line) for line in f if line.strip()]
            raw_threads_for_summary.extend(raw_threads)
            for thread in raw_threads:
                thread_title_str = thread.get('title', 'N/A')
                thread_title_lower = thread_title_str.lower()
                thread_hash = thread.get('thread_hash')
                for post in thread.get('posts', []):
                    original_content, author = post.get('content', ''), post.get('author', 'unknown')
                    search_text = thread_title_lower + " " + original_content.lower()
                    leaked_data_type = classify_content(search_text)
                    risk_level = "Informational"
                    if leaked_data_type in ["데이터베이스/정보 유출", "기업 기밀/문서", "해킹/익스플로잇", "악성코드/도구"]: risk_level = "Critical"
                    elif leaked_data_type in ["스팸/마케팅", "부정/비리"]: risk_level = "Warning"
                    post_url = post.get('post_url', str(uuid.uuid4()))
                    item_id = hashlib.sha256(post_url.encode()).hexdigest()
                    iso_timestamp = parse_posted_at(post.get('posted_at', ''), thread.get('fetched_at', ''))
                    clean_item = {
                        "id": item_id, "account": author, "host": thread.get('source', 'unknown'),
                        "leaked_data": leaked_data_type, "risk_level": risk_level,
                        "date": iso_timestamp, "raw_content": original_content,
                        "memo": memos.get(item_id, ""), "thread_hash": thread_hash,
                        "thread_title": thread_title_str
                    }
                    clean_data.append(clean_item)
        except FileNotFoundError:
            app.logger.error(f"데이터 파일 없음: '{file_path}'")
            continue
    return clean_data, raw_threads_for_summary

# --- 실시간 감지 및 스트리밍 로직 ---
def process_data_and_stream():
    processed_ids = get_processed_ids()
    app.logger.info(f"백그라운드 처리 시작. 현재까지 처리된 ID: {len(processed_ids)}개")
    while True:
        all_clean_data, _ = load_and_analyze_data(RAW_DATA_JSONL_FILES)
        new_items_found = False
        for item in all_clean_data:
            if item['id'] not in processed_ids:
                new_items_found = True; app.logger.info(f"새로운 유출 정보 감지 (ID: {item['id']})")
                create_github_issue(item); yield f"data: {json.dumps(item)}\n\n"
                mark_id_as_processed(item['id']); processed_ids.add(item['id'])
        if not new_items_found: app.logger.info("새로운 유출 정보 없음. 60초 후 다시 확인합니다.")
        time.sleep(60)

# --- Flask 라우트(경로) 설정 ---
@app.route('/')
def index(): return render_template('index.html')

@app.route('/get_initial_data')
def get_initial_data():
    all_data, raw_threads = load_and_analyze_data(RAW_DATA_JSONL_FILES)
    all_data.sort(key=lambda x: x['date'], reverse=True)
    return jsonify({"alerts": all_data, "raw_threads": raw_threads})

@app.route('/stream_alerts')
def stream_alerts(): return Response(process_data_and_stream(), mimetype='text/event-stream')

@app.route('/save_memo', methods=['POST'])
def save_memo():
    data = request.get_json(); alert_id = data.get('id'); memo_text = data.get('memo')
    if not alert_id: return jsonify({"status": "error", "message": "ID is missing"}), 400
    memos[alert_id] = memo_text
    with open(MEMOS_FILE, 'w', encoding='utf-8') as f: json.dump(memos, f, indent=4, ensure_ascii=False)
    return jsonify({"status": "success", "message": "Memo saved"})

if __name__ == '__main__':
    app.run(debug=True, threaded=True)