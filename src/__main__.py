import os
import time
import json
import requests
import feedparser
from datetime import datetime
import pytz
import hashlib

BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

RSS_URLS = [
    'https://www.info.gov.hk/gia/rss/general_zh.xml'
]

HASH_FILE = 'sent_hashes.json'
try:
    with open(HASH_FILE, 'r', encoding='utf-8') as f:
        SENT_HASHES = set(json.load(f))
except (FileNotFoundError, json.JSONDecodeError):
    SENT_HASHES = set()

def save_sent_hashes():
    with open(HASH_FILE, 'w', encoding='utf-8') as f:
        json.dump(list(SENT_HASHES), f, ensure_ascii=False)

def get_hash(text):
    return hashlib.md5(text.encode('utf-8')).hexdigest()

def send_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text}
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
            uid = get_hash(link)

            if uid not in SENT_HASHES:
                msg = f"[新聞稿] [{title}]({link})\n🔗 [更多詳情請見](https://www.isdnews.gov.hk/subscriber/loginpage)"
                send_message(msg)
                SENT_HASHES.add(uid)
    save_sent_hashes()

    if now.strftime("%H:%M") == "12:00":
        send_message("我還活著，請放心！")

def check_for_clear_command():
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    try:
        resp = requests.get(url).json()
        for result in resp.get("result", []):
            if "message" in result and "text" in result["message"]:
                text = result["message"]["text"]
                if text.strip() == "/clear":
                    SENT_HASHES.clear()
                    save_sent_hashes()
                    send_message("✅ 已清除所有已記錄的新聞發送紀錄。")
    except Exception as e:
        print("讀取命令錯誤：", e)

while True:
    hk_time = datetime.now(pytz.timezone("Asia/Hong_Kong"))
    if (hk_time.hour > 8 or (hk_time.hour == 8 and hk_time.minute >= 30)) and (hk_time.hour < 24 or (hk_time.hour == 0 and hk_time.minute <= 15)):
        check_for_clear_command()
        fetch_and_send()
    time.sleep(60)
