
from crawler import run_crawler
from analyzer import run_analyzer
from alert import send_alert

def main():
    print("[*] System running.. ")

    # Crawlering
    data = run_crawler()

    # analyzer
    result = run_analyzer(data)

    # alert
    send_alert(result)

    print("[+] ...Done ")

if __name__ == "__main__":
    main()