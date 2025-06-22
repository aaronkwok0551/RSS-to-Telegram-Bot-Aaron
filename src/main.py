import os
import time
import json
import requests
import feedparser
from datetime import datetime
import pytz

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

SENT_FILE = "sent_urls.json"
try:
    with open(SENT_FILE, "r", encoding="utf-8") as f:
        SENT_URLS = set(json.load(f))
except (FileNotFoundError, json.JSONDecodeError):
    SENT_URLS = set()

def save_sent_urls():
    with open(SENT_FILE, "w", encoding="utf-8") as f:
        json.dump(list(SENT_URLS), f, ensure_ascii=False)

def send_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
    try:
        requests.post(url, data=payload)
    except Exception as e:
        print(f"發送錯誤: {e}")

def fetch_and_send():
    print("🔍 正在檢查新聞…", datetime.now(pytz.timezone("Asia/Hong_Kong")).strftime("%H:%M:%S"))

    sources = [
        ("新聞稿", "https://www.info.gov.hk/gia/rss/general_zh.xml"),
        ("RTHK", "https://rthk.hk/rthk/news/rss/c_expressnews_clocal.xml")
    ]

    for label, rss_url in sources:
        print(f"📡 檢查中：{label}")
        feed = feedparser.parse(rss_url)
        new_items = []

        for entry in feed.entries[:10]:
            title = entry.title
            link = entry.link

            if link not in SENT_URLS:
                new_items.append((title, link))
                SENT_URLS.add(link)

        if new_items:
            if "rthk" in rss_url:
                message = "*【RTHK 即時新聞】*\n"
                for i, (title, link) in enumerate(new_items, start=1):
                    message += f"{i}. [{title}]({link})\n"
                message += f"\n🕓 更新時間：{datetime.now(pytz.timezone('Asia/Hong_Kong')).strftime('%Y-%m-%d %H:%M:%S')}"
                send_message(message)
            else:
                for title, link in new_items:
                    msg = f"*{title}*\n[🔗 點此查看新聞]({link})"
                    msg += "\n\n👉 [更多詳情請見 ISD 官網](https://www.isdnews.gov.hk/subscriber/loginpage)"
                    send_message(msg)

    save_sent_urls()

    now = datetime.now(pytz.timezone("Asia/Hong_Kong"))
    if now.strftime("%H:%M") == "12:00":
        send_message("✅ 我還活著，請放心！")

def check_clear_command():
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    try:
        response = requests.get(url).json()
        for update in response.get("result", []):
            message = update.get("message", {}).get("text", "")
            if message.strip() == "/clear":
                SENT_URLS.clear()
                save_sent_urls()
                send_message("🧹 已清空已發送紀錄")
                break
    except Exception as e:
        print(f"檢查清除指令錯誤: {e}")

while True:
    hk_time = datetime.now(pytz.timezone("Asia/Hong_Kong"))
    if (hk_time.hour > 8 or (hk_time.hour == 8 and hk_time.minute >= 30)) and (hk_time.hour < 24 or (hk_time.hour == 0 and hk_time.minute <= 15)):
        check_clear_command()
        fetch_and_send()
    time.sleep(60)
