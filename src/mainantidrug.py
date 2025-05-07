import os
import json
import time
import requests
import feedparser
from datetime import datetime, timedelta
from time import mktime
import pytz

# Google News RSS 搜尋
RSS_URLS = [
    "https://news.google.com/rss/search?q=毒品+when:1d&hl=zh-HK&gl=HK&ceid=HK:zh-Hant",
    "https://news.google.com/rss/search?q=太空油+when:1d&hl=zh-HK&gl=HK&ceid=HK:zh-Hant",
    "https://news.google.com/rss/search?q=依託咪酯+when:1d&hl=zh-HK&gl=HK&ceid=HK:zh-Hant",
    "https://news.google.com/rss/search?q=海關+when:1d&hl=zh-HK&gl=HK&ceid=HK:zh-Hant"
]

KEYWORDS = ["毒品", "依託咪酯", "太空油", "海關"]
SENT_TITLES_FILE = 'sent_titles.json'

# 狀態控制
active = True
last_alive_hour = None
last_update_id = None

# 載入已傳送過的標題
try:
    with open(SENT_TITLES_FILE, 'r', encoding='utf-8') as f:
        SENT_TITLES = set(json.load(f))
except:
    SENT_TITLES = set()

def save_sent_titles():
    with open(SENT_TITLES_FILE, 'w', encoding='utf-8') as f:
        json.dump(list(SENT_TITLES), f, ensure_ascii=False)

# 傳送 Telegram 訊息
def send_message(text):
    url = f"https://api.telegram.org/bot{os.environ['BOT_TOKEN']}/sendMessage"
    payload = {
        "chat_id": os.environ["CHAT_ID"],
        "text": text,
        "disable_web_page_preview": True
    }
    try:
        requests.post(url, data=payload)
    except:
        print("無法發送訊息")

# 檢查指令
def check_command():
    global active, last_update_id
    url = f"https://api.telegram.org/bot{os.environ['BOT_TOKEN']}/getUpdates"
    try:
        res = requests.get(url).json()
        if not res["ok"]:
            return
        for update in res["result"]:
            update_id = update["update_id"]
            if last_update_id is not None and update_id <= last_update_id:
                continue
            if "message" in update and str(update["message"]["chat"]["id"]) == os.environ["CHAT_ID"]:
                text = update["message"].get("text", "")
                if text == "/pause":
                    active = False
                    send_message("已暫停自動推送")
                elif text == "/start":
                    active = True
                    send_message("已重新啟動自動推送")
                elif text == "/status":
                    state = "啟動中" if active else "已暫停"
                    send_message(f"目前狀態：{state}")
            last_update_id = update_id
    except:
        print("檢查控制指令失敗")

# 報平安
def check_alive():
    global last_alive_hour
    now = datetime.now(pytz.timezone("Asia/Hong_Kong"))
    if now.hour == 12 and last_alive_hour != 12:
        send_message("我還活著，請放心！")
        last_alive_hour = 12
    elif now.hour != 12:
        last_alive_hour = None

# 搜尋新聞
def fetch_and_send():
    now = datetime.now(pytz.timezone("Asia/Hong_Kong"))
    feed = feedparser.parse("https://news.google.com/rss/search?q=毒品+OR+依託咪酯+OR+太空油+OR+海關&hl=zh-HK&gl=HK&ceid=HK:zh-Hant")
    
    entries = sorted(feed.entries, key=lambda x: x.published_parsed, reverse=True)
    new_items = []
    
    for entry in entries:
        title = entry.title.strip()
        link = entry.link.strip()
        if title not in SENT_TITLES:
            new_items.append(f"{len(new_items)+1}. {title}\n{link}")
            SENT_TITLES.add(title)
        if len(new_items) >= 10:
            break

    if new_items:
        message = "【禁毒/海關新聞】\n" + "\n\n".join(new_items)
        send_message(message)
        save_sent_titles()
    entries.sort(key=lambda x: x["time"], reverse=True)
    if entries:
        message = "【禁毒/海關新聞】\n"
        for i, item in enumerate(entries, 1):
            message += f"{i}. {item['title']}\n{item['link']}\n"
        send_message(message)

# 主循環
while True:
    now = datetime.now(pytz.timezone("Asia/Hong_Kong"))
    if 8 <= now.hour < 24 or (now.hour == 0 and now.minute <= 15):
        check_command()
        check_alive()
        if active:
            fetch_and_send()
    else:
        print("不在指定運作時段")
    time.sleep(60)
