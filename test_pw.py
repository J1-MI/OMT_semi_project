import sys
print("python:", sys.executable)

# 어떤 playwright가 import 되는지 경로 확인
import importlib, importlib.util
spec = importlib.util.find_spec("playwright")
print("playwright module spec:", spec.origin if spec else None)

try:
    from importlib.metadata import version
    print("playwright version:", version("playwright"))
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        print("browsers:", [p.chromium.name, p.firefox.name, p.webkit.name])
    print("OK")
except Exception as e:
    import traceback
    traceback.print_exc()
