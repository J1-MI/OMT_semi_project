import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from backend.crawler import run_crawler
from backend.analyzer import run_analyzer
from backend.alert import send_alert
from backend.osint import run_osint

from flask import Flask, render_template, redirect, url_for

template_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'templates'))
app = Flask(__name__, template_folder=template_path)

@app.route('/')
def index():
    return render_template("index.html")

@app.route('/crawl')
def crawl():
    data = run_crawler()
    result = run_analyzer(data)
    send_alert(result)
    return render_template("result.html", result=result)

@app.route('/osint')
def osint():
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    input_dir = os.path.join(root, 'data', 'crawled')
    out_path  = os.path.join(root, 'data', 'osint_out.jsonl')
    os.makedirs(input_dir, exist_ok=True)
    result = run_osint(input_dir, out_path)
    return render_template("result.html", result=result)

if __name__ == '__main__':
    app.run(debug=True)