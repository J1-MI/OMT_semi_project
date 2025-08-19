# backend/alert/alert_system/bp.py
import os
from flask import Blueprint, render_template, jsonify, Response, request
from backend.alert.alert_system.core import (
    get_initial_data_payload,
    process_data_and_stream,
    memos, MEMOS_FILE,
)

BASE_DIR = os.path.dirname(__file__)
ROOT_DIR = os.path.abspath(os.path.join(BASE_DIR, '..', '..', '..'))
TEMPLATE_DIR = os.path.join(ROOT_DIR, 'templates')

alerts_bp = Blueprint(
    "alerts",
    __name__,
    template_folder=TEMPLATE_DIR,   # 루트 templates 사용
)

@alerts_bp.route("/")
def index():
    return render_template("index.html")   # 루트 templates/index.html

@alerts_bp.route("/get_initial_data")
def get_initial_data():
    return jsonify(get_initial_data_payload())

@alerts_bp.route("/stream_alerts")
def stream_alerts():
    return Response(process_data_and_stream(), mimetype="text/event-stream")

@alerts_bp.route("/save_memo", methods=["POST"])
def save_memo():
    data = request.get_json() or {}
    alert_id = data.get("id"); memo_text = data.get("memo", "")
    if not alert_id:
        return jsonify({"status": "error", "message": "ID is missing"}), 400
    memos[alert_id] = memo_text
    with open(MEMOS_FILE, 'w', encoding='utf-8') as f:
        import json
        json.dump(memos, f, indent=4, ensure_ascii=False)
    return jsonify({"status": "success", "message": "Memo saved"})
