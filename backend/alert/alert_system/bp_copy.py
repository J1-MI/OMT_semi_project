import os, json
from flask import Blueprint, render_template, jsonify

# 블루프린트 생성 (템플릿 폴더는 alerts 하위로 네임스페이스 분리)
bp = Blueprint(
    "alerts",
    __name__,
    template_folder="templates",   # 내부에서 'alerts/index.html'로 렌더링
    static_folder=None,
)

BASE_DIR = os.path.dirname(__file__)
# alerts.jsonl(또는 .json) 파일 경로
ALERTS_PATH = os.path.join(BASE_DIR, "alerts.jsonl")

def _read_alerts():
    """alerts.jsonl(.json) 둘 다 지원. 없으면 빈 리스트."""
    data = []
    if not os.path.exists(ALERTS_PATH):
        return data
    with open(ALERTS_PATH, "r", encoding="utf-8", errors="ignore") as f:
        first = f.read(1)
        f.seek(0)
        if first == "[":
            try:
                data = json.load(f)
            except Exception:
                data = []
        else:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data.append(json.loads(line))
                except Exception:
                    continue
    return data

@bp.route("/")
def index():
    alerts = _read_alerts()
    return render_template("alerts/index.html", alerts=alerts, count=len(alerts))

@bp.route("/api/alerts")
def api_alerts():
    return jsonify(_read_alerts())
