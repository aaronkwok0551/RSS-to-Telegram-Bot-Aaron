import os
import time
import json
import requests
import feedparser
from datetime import datetime
import pytz
from flask import Flask, request

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

RSS_URLS = [
    'https://www.info.gov.hk/gia/rss/general_zh.xml',
    'https://rthk.hk/rthk/news/rss/c_expressnews_clocal.xml'
]

SENT_TITLES_FILE = 'sent_titles.json'
try:
    with open(SENT_TITLES_FILE, 'r', encoding='utf-8') as f:
        SENT_LINKS = set(json.load(f))
except (FileNotFoundError, json.JSONDecodeError):
    SENT_LINKS = set()

def save_sent_links():
    with open(SENT_TITLES_FILE, 'w', encoding='utf-8') as f:
        json.dump(list(SENT_LINKS), f, ensure_ascii=False)

def send_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
        "parse_mode": "Markdown"
    }
    try:
        requests.post(url, data=payload)
    except requests.exceptions.RequestException as e:
        print(f"發送訊息失敗：{e}")

def format_message(source, title, link):
    if "info.gov.hk" in link:
        return f"*【新聞稿】* [{title}]({link})\n[更多詳情請見 ISD 網站](https://www.isdnews.gov.hk/subscriber/loginpage)"
    elif "rthk.hk" in link:
        return f"*【香港電台新聞】* [{title}]({link})"
    else:
        return f"[{title}]({link})"

def fetch_and_send():
    now = datetime.now(pytz.timezone("Asia/Hong_Kong"))
    print("檢查中…", now.strftime("%Y-%m-%d %H:%M:%S"))

    for rss in RSS_URLS:
        feed = feedparser.parse(rss)
        for entry in feed.entries[:10]:
            title = entry.title
            link = entry.link
            if link not in SENT_LINKS:
                msg = format_message(rss, title, link)
                send_message(msg)
                SENT_LINKS.add(link)
    save_sent_links()

    if now.strftime("%H:%M") == "12:00":
        send_message("我還活著，請放心！")

@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    data = request.get_json()
    if not data or "message" not in data:
        return "OK"
    message = data["message"]
    text = message.get("text", "")
    chat_id = str(message["chat"]["id"])
    if text == "/clear" and chat_id == CHAT_ID:
        SENT_LINKS.clear()
        save_sent_links()
        send_message("✅ 已清空已發送的紀錄")
    return "OK"

if __name__ == "__main__":
    while True:
        hk_time = datetime.now(pytz.timezone("Asia/Hong_Kong"))
        if (
            (hk_time.hour > 8 or (hk_time.hour == 8 and hk_time.minute >= 30)) and
            (hk_time.hour < 24 or (hk_time.hour == 0 and hk_time.minute <= 15))
        ):
            fetch_and_send()
        time.sleep(60)
