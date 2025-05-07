import os
import time
import json
import feedparser
import requests
import pytz
from datetime import datetime

GOOGLE_NEWS_RSS_URL = "https://news.google.com/rss/search?q=毒品+OR+依託咪酯+OR+太空油+OR+海關&hl=zh-HK&gl=HK&ceid=HK:zh-Hant"

SENT_TITLES_FILE = 'sent_titles.json'
try:
    with open(SENT_TITLES_FILE, 'r', encoding='utf-8') as f:
        SENT_TITLES = set(json.load(f))
except (FileNotFoundError, json.JSONDecodeError):
    SENT_TITLES = set()

def save_sent_titles():
    with open(SENT_TITLES_FILE, 'w', encoding='utf-8') as f:
        json.dump(list(SENT_TITLES), f, ensure_ascii=False, indent=2)

def send_message(text):
    url = "https://api.telegram.org/bot" + os.environ["BOT_TOKEN"] + "/sendMessage"
    payload = {
        "chat_id": os.environ["CHAT_ID"],
        "text": text,
        "disable_web_page_preview": True
    }
    try:
        requests.post(url, data=payload)
    except requests.exceptions.RequestException as e:
        print(f"發送訊息錯誤: {e}")

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
        send_message("【禁毒／海關新聞】\n" + "\n\n".join(messages))
        save_sent_titles()

while True:
    hk_time = datetime.now(pytz.timezone("Asia/Hong_Kong"))
    if 8 <= hk_time.hour < 24 or (hk_time.hour == 0 and hk_time.minute <= 15):
        fetch_and_send()
    time.sleep(60)
