import os
import time
import json
import requests
import feedparser
from datetime import datetime
import pytz
from flask import Flask, request

# 設定 Flask App（用於接受 /clear 指令）
app = Flask(__name__)

# Telegram Bot Token 和 Chat ID
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

# RSS 來源列表
RSS_URLS = [
    'https://www.info.gov.hk/gia/rss/general_zh.xml',
    'https://rthk.hk/rthk/news/rss/c_expressnews_clocal.xml'
]

# 已發送新聞的 link 集合
SENT_LINKS_FILE = 'sent_links.json'
try:
    with open(SENT_LINKS_FILE, 'r', encoding='utf-8') as f:
        SENT_LINKS = set(json.load(f))
except (FileNotFoundError, json.JSONDecodeError):
    SENT_LINKS = set()

def save_sent_links():
    with open(SENT_LINKS_FILE, 'w', encoding='utf-8') as f:
        json.dump(list(SENT_LINKS), f, ensure_ascii=False)

def send_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown", "disable_web_page_preview": True}
    try:
        requests.post(url, data=payload)
    except requests.exceptions.RequestException as e:
        print(f"發送訊息時發生錯誤：{e}")

def fetch_and_send():
    now = datetime.now(pytz.timezone("Asia/Hong_Kong"))
    print("檢查中…", now.strftime("%Y-%m-%d %H:%M:%S"))

    for rss in RSS_URLS:
        feed = feedparser.parse(rss)
        for entry in feed.entries[:10]:
            title = entry.title
            link = entry.link

            if link not in SENT_LINKS:
                if "info.gov.hk" in rss:
                    # 政府新聞稿
                    msg = f"[政府新聞稿] [{title}]({link})\n🔗 [更多詳情請見 ISD 登入頁面](https://www.isdnews.gov.hk/subscriber/loginpage)"
                elif "rthk.hk" in rss:
                    # 香港電台
                    msg = f"[香港電台新聞] [{title}]({link})"
                else:
                    msg = f"[新聞] [{title}]({link})"

                send_message(msg)
                SENT_LINKS.add(link)

    save_sent_links()

    # 每天中午發送「我還活著」
    if now.strftime("%H:%M") == "12:00":
        send_message("我還活著，請放心！")

# Flask 路由處理 /clear 指令
@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def telegram_webhook():
    data = request.json
    if "message" in data and "text" in data["message"]:
        chat_id = str(data["message"]["chat"]["id"])
        if data["message"]["text"].strip() == "/clear" and chat_id == CHAT_ID:
            SENT_LINKS.clear()
            save_sent_links()
            send_message("已清空推送紀錄 ✅")
    return "OK"

# 主程式
if __name__ == "__main__":
    from threading import Thread

    def polling_loop():
        while True:
            hk_time = datetime.now(pytz.timezone("Asia/Hong_Kong"))
            if (hk_time.hour > 8 or (hk_time.hour == 8 and hk_time.minute >= 30)) and \
               (hk_time.hour < 24 or (hk_time.hour == 0 and hk_time.minute <= 15)):
                fetch_and_send()
            time.sleep(60)

    # 啟動輪詢程式
    Thread(target=polling_loop).start()

    # 啟動 Flask Webhook
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
