from flask import Flask, render_template

app = Flask(__name__)

# 임시 더미 알림 데이터
alerts = [
    {"title": "위협 감지", "message": "의심스러운 활동이 포착되었습니다.", "level": "High"},
    {"title": "업데이트 알림", "message": "데이터베이스가 갱신되었습니다.", "level": "Info"},
    {"title": "에러 발생", "message": "크롤링 중 오류가 발생했습니다.", "level": "Error"},
]

@app.route("/")
def home():
    return "<h2>알림 시스템 초기 화면</h2><p><a href='/alerts'>→ 알림 보러 가기</a></p>"

@app.route("/alerts")
def alert_page():
    return render_template("alerts.html", alerts=alerts)

if __name__ == "__main__":
    app.run(debug=True)
