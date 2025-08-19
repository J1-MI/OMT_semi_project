import requests

def create_github_issue(item):
    """
    주어진 item(dict)을 바탕으로 GitHub Issue 생성
    """
    # 지금은 테스트니까 그냥 로그 찍기
    print(f"[DEBUG] GitHub Issue 생성 요청: {item['thread_title']}")
    return {"status": "ok"}