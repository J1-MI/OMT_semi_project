
from pathlib import Path
from datetime import datetime, UTC
from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parent.parent / "out"
OUT.mkdir(exist_ok=True)

def crawl(url: str):
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            proxy={"server": "socks5://127.0.0.1:9050"},
            args=[
                "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1",
                "--proxy-bypass-list=<-loopback>"
            ]
        )
        page = browser.new_page()
        page.set_default_timeout(45000)

        page.goto(url, wait_until="domcontentloaded")
        try:
            # JS 무거운 사이트 고려해 잠깐 더 대기
            page.wait_for_timeout(3000)
        except:
            pass

        title = (page.title() or "").strip()[:200]
        html = page.content()
        ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")

        # 저장 파일명
        safe = url.replace("://", "_").replace("/", "_")
        (OUT / f"{safe}_{ts}.html").write_text(html, encoding="utf-8")
        page.screenshot(path=OUT / f"{safe}_{ts}.png", full_page=True)

        browser.close()
        return {"url": url, "title": title, "saved_at": str(OUT.resolve())}

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python crawl_one.py <onion_url>")
        sys.exit(1)
    info = crawl(sys.argv[1])
    print("DONE:", info)
