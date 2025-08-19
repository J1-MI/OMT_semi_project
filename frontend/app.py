import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from backend.crawler import run_crawler
from backend.analyzer import run_analyzer
from backend.alert import send_alert
from backend.osint import run_osint

from flask import Flask, render_template, redirect, url_for
#from backend.alert import alerts_bp
from backend.alert.alert_system.bp import alerts_bp

template_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'templates'))
app = Flask(__name__, template_folder=template_path)
app.register_blueprint(alerts_bp, url_prefix="/alerts")

@app.route('/')
def index():
    return render_template("index.html")

@app.route('/osint')
def osint():
    input_dir = "./data/crawled"            # 크롤러/OSINT 원본 파일 경로(팀 규칙)
    out_path  = "./data/osint_out.jsonl"    # 결과 저장 경로
    result = run_osint(input_dir, out_path)
    return render_template("result.html", result=result)

@app.route('/crawl')
def crawl():
    # 1) 크롤링
    crawl = run_crawler(
        config_path="crawler/src/crawler/selectors.yaml",
        forums=("darkforums",),    # 필요 시 여러 개
        pages=1,
        engine="requests",
        use_tor=False,             # Tor 쓰면 True
        out_jsonl="data/crawled/crawl.jsonl",
    )
    # 2) 결과 요약
    result = {"crawl_ok": crawl["ok"], "out_jsonl": crawl["out_jsonl"]}

    # 3) (선택) 바로 OSINT 파이프라인까지 실행
    if crawl["ok"] and crawl["out_jsonl"]:
        # 폴더 전체를 처리하므로 input_dir만 넘김
        osint = run_osint("./data/crawled", "./data/osint_out.jsonl")
        result.update({"osint": osint})

    return render_template("result.html", result=result)



if __name__ == '__main__':
    app.run(debug=True)