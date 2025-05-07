
import os
import time
import json
import feedparser
from datetime import datetime
import pytz
import requests

# RSS 來源（Google News 關鍵詞搜尋）
GOOGLE_NEWS_RSS_URL = "https://news.google.com/rss/search?q=%E6%AF%92%E5%93%81+OR+%E4%BE%9D%E8%A8%97%E5%92%AA%E9%85%8F+OR+%E5%A4%AA%E7%A9%BA%E6%B2%B9+OR+%E6%B5%B7%E9%97%9C&hl=zh-HK&gl=HK&ceid=HK:zh-Hant"

# 儲存已發送標題
SENT_TITLES_FILE = 'sent_titles_antidrug.json'
try:
    with open(SENT_TITLES_FILE, 'r', encoding='utf-8') as f:
        SENT_TITLES = set(json.load(f))
except (FileNotFoundError, json.JSONDecodeError):
    SENT_TITLES = set()

def save_sent_titles():
    with open(SENT_TITLES_FILE, 'w', encoding='utf-8') as f:
        json.dump(list(SENT_TITLES), f, ensure_ascii=False)

# 發送訊息
def send_message(text):
    url = f"https://api.telegram.org/bot{os.environ['BOT_TOKEN']}/sendMessage"
    payload = {
        "chat_id": os.environ["CHAT_ID"],
        "text": text,
        "disable_web_page_preview": True
    }
    try:
        requests.post(url, data=payload)
    except requests.exceptions.RequestException as e:
        print(f"發送錯誤: {e}")

# 抓取並發送最新新聞
def fetch_and_send():
    feed = feedparser.parse(GOOGLE_NEWS_RSS_URL)

    # 過濾有發佈時間的新聞並依時間排序
    entries = [e for e in feed.entries if hasattr(e, "published_parsed")]
    entries.sort(key=lambda x: x.published_parsed, reverse=True)

    messages = []
    for entry in entries:
        title = entry.title.strip()
        link = entry.link.strip()
        if title not in SENT_TITLES:
            messages.append(f"{len(messages)+1}. {title}\n{link}")
            SENT_TITLES.add(title)
        if len(messages) >= 10:
            break

    if messages:
        send_message("【禁毒／海關新聞】" + "\n" + "\n".join(messages))

" + "

send_message("【禁毒／海關新聞】\n" + "\n".join(messages))
        save_sent_titles()

# 主程序
if __name__ == "__main__":
    while True:
        hk_time = datetime.now(pytz.timezone("Asia/Hong_Kong"))
        if 8 <= hk_time.hour < 24:
            fetch_and_send()
        time.sleep(60)
