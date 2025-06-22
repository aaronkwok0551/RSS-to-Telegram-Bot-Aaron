import os
import time
import json
import requests
import feedparser
from datetime import datetime
import pytz
from flask import Flask, request

app = Flask(__name__)

# Telegram Bot Token 和 Chat ID 從環境變數讀取
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

# RSS來源列表
RSS_URLS = [
    'https://www.info.gov.hk/gia/rss/general_zh.xml',
    'https://rthk.hk/rthk/news/rss/c_expressnews_clocal.xml'
]

# 已發送新聞的 URL 集合
SENT_URLS_FILE = 'sent_urls.json'
try:
    with open(SENT_URLS_FILE, 'r', encoding='utf-8') as f:
        SENT_URLS = set(json.load(f))
except (FileNotFoundError, json.JSONDecodeError):
    SENT_URLS = set()

def save_sent_urls():
    with open(SENT_URLS_FILE, 'w', encoding='utf-8') as f:
        json.dump(list(SENT_URLS), f, ensure_ascii=False)

def send_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}
    try:
        requests.post(url, data=payload)
    except requests.exceptions.RequestException as e:
        print(f"發送訊息時發生錯誤：{e}")

def fetch_and_send():
    now = datetime.now(pytz.timezone("Asia/Hong_Kong"))
    print("開始檢查 RSS 頻道於", now.strftime("%Y-%m-%d %H:%M:%S"))

    for rss in RSS_URLS:
        feed = feedparser.parse(rss)
        print(f"正在檢查: {'新聞稿' if 'info.gov.hk' in rss else 'RTHK新聞'}")
        for entry in feed.entries[:10]:
            title = entry.title
            link = entry.link
            if link not in SENT_URLS:
                if "info.gov.hk" in rss:
                    msg = f"*{title}*\n[🔗 查看新聞稿]({link})\n\n[更多詳情請見 ISD 網站](https://www.isdnews.gov.hk/subscriber/loginpage)"
                else:
                    msg = f"*{title}*\n[🔗 查看新聞]({link})"
                send_message(msg)
                SENT_URLS.add(link)
    save_sent_urls()

    if now.strftime("%H:%M") == "12:00":
        send_message("🕛 我還活著，請放心！")

def is_within_active_hours():
    hk_time = datetime.now(pytz.timezone("Asia/Hong_Kong"))
    return (
        (hk_time.hour > 8 or (hk_time.hour == 8 and hk_time.minute >= 30))
        and (hk_time.hour < 24 or (hk_time.hour == 0 and hk_time.minute <= 15))
    )

@app.route(f'/{BOT_TOKEN}', methods=['POST'])
def webhook():
    data = request.get_json()
    if 'message' in data:
        msg_text = data['message'].get('text', '')
        if msg_text.strip() == '/clear':
            SENT_URLS.clear()
            save_sent_urls()
            send_message("✅ 已清空發送紀錄。")
    return "ok"

if __name__ == "__main__":
    # 若你使用的是 Railway 或類似平台，請開啟 webhook listener
    from threading import Thread

    def polling_loop():
        while True:
            if is_within_active_hours():
                fetch_and_send()
            time.sleep(60)

    # Telegram webhook 接收訊息
    from waitress import serve
    import threading

    polling_thread = threading.Thread(target=polling_loop)
    polling_thread.daemon = True
    polling_thread.start()

    serve(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
