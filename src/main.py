import time
import pytz
from datetime import datetime
import requests
import feedparser
import json

# === 基本設定 ===
RSS_URLS = [
    "https://www.info.gov.hk/gia/rss/general_zh.xml",  # 政府新聞
    "https://rthk.hk/rthk/news/rss/c_expressnews_clocal.xml"  # RTHK
]

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
        "parse_mode": "HTML"
    }
    try:
        requests.post(url, data=payload)
    except Exception as e:
        print(f"發送訊息錯誤: {e}")

def fetch_and_send():
    now = datetime.now(pytz.timezone("Asia/Hong_Kong"))
    print(f"檢查時間：{now.strftime('%H:%M')}")

    for rss_url in RSS_URLS:
        feed = feedparser.parse(rss_url)
        for entry in feed.entries:
            title = entry.title
            link = entry.link
            if title not in SENT_TITLES:
                msg = f"【新聞推送】\n{title}\n{link}"
                if "info.gov.hk" in rss_url:
                    msg += "\n更多詳情請見：https://www.isdnews.gov.hk/subscriber/loginpage"
                send_message(msg)
                SENT_TITLES.add(title)

    save_sent_titles()

# 每天中午發送「我還活著」訊息
def noon_check():
    now = datetime.now(pytz.timezone("Asia/Hong_Kong"))
    if now.strftime("%H:%M") == "12:00":
        send_message("我還活著，請放心！")

# === 主循環 ===
while True:
    hk_time = datetime.now(pytz.timezone("Asia/Hong_Kong"))
    if (hk_time.hour > 8 or (hk_time.hour == 8 and hk_time.minute >= 30)) and (hk_time.hour < 24 or (hk_time.hour == 0 and hk_time.minute <= 15)):
        noon_check()
        fetch_and_send()
    time.sleep(60)
