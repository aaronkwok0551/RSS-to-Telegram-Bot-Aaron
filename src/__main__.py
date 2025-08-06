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
UPDATE_ID_FILE = "last_update_id.json"

# 載入已發送連結
try:
    with open(SENT_FILE, "r", encoding="utf-8") as f:
        SENT_URLS = set(json.load(f))
except:
    SENT_URLS = set()

# 載入最後處理過的 Telegram update_id
try:
    with open(UPDATE_ID_FILE, "r") as f:
        LAST_UPDATE_ID = json.load(f)
except:
    LAST_UPDATE_ID = 0

def save_sent_urls():
    with open(SENT_FILE, "w", encoding="utf-8") as f:
        json.dump(list(SENT_URLS), f, ensure_ascii=False)

def save_update_id(update_id):
    with open(UPDATE_ID_FILE, "w", encoding="utf-8") as f:
        json.dump(update_id, f)

def send_message(text, disable_preview=False):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": disable_preview
    }
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
        new_messages = []

        for entry in feed.entries[:10]:
            title = entry.title
            link = entry.link

            if link not in SENT_URLS:
                if "info.gov.hk" in rss_url:
                    msg = f"*{title}*\n[🔗 點此查看新聞]({link})\n\n👉 [GNMIS](https://www.isdnews.gov.hk/subscriber/loginpage)"
                    send_message(msg, disable_preview=False)
                elif "rthk.hk" in rss_url:
                    new_messages.append(f"• [{title}]({link})")
                SENT_URLS.add(link)

        # RTHK 整批發送
        if "rthk.hk" in rss_url and new_messages:
            full_message = "*📻 RTHK 新聞摘要：*\n" + "\n".join(new_messages)
            send_message(full_message, disable_preview=True)

    save_sent_urls()

    now = datetime.now(pytz.timezone("Asia/Hong_Kong"))
    if now.strftime("%H:%M") == "12:00":
        send_message("✅ 我還活著，請放心！", disable_preview=True)

def check_clear_command():
    global LAST_UPDATE_ID
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    try:
        response = requests.get(url).json()
        for update in response.get("result", []):
            update_id = update["update_id"]
            if update_id <= LAST_UPDATE_ID:
                continue  # 已處理過，跳過

            message = update.get("message", {}).get("text", "")
            if message.strip() == "/clear":
                SENT_URLS.clear()
                save_sent_urls()
                send_message("🧹 已清空已發送紀錄", disable_preview=True)

            LAST_UPDATE_ID = update_id
            save_update_id(LAST_UPDATE_ID)

    except Exception as e:
        print(f"檢查清除指令錯誤: {e}")

while True:
    hk_time = datetime.now(pytz.timezone("Asia/Hong_Kong"))
    if (hk_time.hour > 8 or (hk_time.hour == 8 and hk_time.minute >= 30)) and (hk_time.hour < 24 or (hk_time.hour == 0 and hk_time.minute <= 15)):
        check_clear_command()
        fetch_and_send()
    time.sleep(60)
