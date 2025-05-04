import os
import json
import time
import requests
import feedparser
from datetime import datetime
import pytz

# RSS 來源列表
RSS_URLS = [
    "https://www.info.gov.hk/gia/rss/general_zh.xml",
    "https://rthk.hk/rthk/news/rss/c_expressnews_clocal.xml"
]

# 儲存已發送標題避免重複
SENT_TITLES_FILE = 'sent_titles.json'
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
    url = "https://api.telegram.org/bot" + os.environ["BOT_TOKEN"] + "/sendMessage"
    payload = {
        "chat_id": os.environ["CHAT_ID"],
        "text": text
    }
    try:
        requests.post(url, data=payload)
    except requests.exceptions.RequestException as e:
        print(f"發送訊息錯誤: {e}")

# 控制啟動狀態
active = True
last_update_id = None

def handle_command(text):
    global active
    if text == "/pause":
        active = False
        send_message("已暫停自動推送")
    elif text == "/start":
        active = True
        send_message("已重新啟動自動推送")

# 檢查是否有新的控制指令
def check_command():
    global last_update_id
    url = f"https://api.telegram.org/bot{os.environ['BOT_TOKEN']}/getUpdates"
    try:
        resp = requests.get(url).json()
        for result in resp.get("result", []):
            update_id = result["update_id"]
            if last_update_id is not None and update_id <= last_update_id:
                continue
            message = result.get("message", {})
            if str(message.get("chat", {}).get("id")) == os.environ['CHAT_ID']:
                text = message.get("text", "")
                handle_command(text)
                last_update_id = update_id
    except Exception as e:
        print(f"檢查控制指令錯誤: {e}")

# 傳送新的新聞
def fetch_and_send():
    now = datetime.now(pytz.timezone("Asia/Hong_Kong"))
    print("檢查時間：", now.strftime("%H:%M"))

    for rss in RSS_URLS:
        feed = feedparser.parse(rss)
        for entry in feed.entries:
            title = entry.title
            link = entry.link

            if title not in SENT_TITLES:
                # 根據 RSS 來源加標籤
                if "info.gov.hk" in rss:
                    prefix = "[新聞稿] "
                    link += "\nhttps://www.isdnews.gov.hk/subscriber/loginpage"
                elif "rthk.hk" in rss:
                    prefix = "[香港電台新聞] "
                else:
                    prefix = ""

                msg = f"{prefix}{title}\n{link}"
                send_message(msg)
                SENT_TITLES.add(title)

    save_sent_titles()

# 每日中午發送 I'm alive 訊息
def check_alive():
    now = datetime.now(pytz.timezone("Asia/Hong_Kong"))
    if now.strftime("%H:%M") == "12:00":
        send_message("我還活著，請放心！")

# 主循環
while True:
    hk_time = datetime.now(pytz.timezone("Asia/Hong_Kong"))
    if 8 <= hk_time.hour < 24 or (hk_time.hour == 0 and hk_time.minute <= 15):
        check_command()
        check_alive()
        if active:
            fetch_and_send()
    time.sleep(60)
