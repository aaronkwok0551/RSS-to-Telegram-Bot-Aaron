
import feedparser
import json
import time
import os
import re
from datetime import datetime
import pytz
import requests

RSS_URLS = [
    ('政府新聞稿', 'https://www.info.gov.hk/gia/rss/general_zh.xml'),
    ('香港電台新聞', 'https://rthk.hk/rthk/news/rss/c_expressnews_clocal.xml')
]

ISD_LINK = "https://www.isdnews.gov.hk/subscriber/loginpage"
SENT_LINKS_FILE = "sent_links_rthk_info.json"

try:
    with open(SENT_LINKS_FILE, "r", encoding="utf-8") as f:
        SENT_LINKS = set(json.load(f))
except (FileNotFoundError, json.JSONDecodeError):
    SENT_LINKS = set()

def save_sent_links():
    with open(SENT_LINKS_FILE, "w", encoding="utf-8") as f:
        json.dump(list(SENT_LINKS), f, ensure_ascii=False, indent=2)

def escape_md(text):
    return re.sub(r'([_*\[\]()~`>#+\-=|{}.!])', r'\\\1', text)

def send_message(text, markdown=True):
    url = f"https://api.telegram.org/bot{os.environ['BOT_TOKEN']}/sendMessage"
    payload = {
        "chat_id": os.environ["CHAT_ID"],
        "text": text,
        "disable_web_page_preview": True
    }
    if markdown:
        payload["parse_mode"] = "MarkdownV2"
    try:
        requests.post(url, data=payload)
    except requests.exceptions.RequestException as e:
        print(f"發送訊息錯誤: {e}")

def fetch_and_send():
    for name, url in RSS_URLS:
        feed = feedparser.parse(url)
        print(f"【{name}】 抓到 {len(feed.entries)} 條")
        entries = [e for e in feed.entries if hasattr(e, "published_parsed")]
        entries.sort(key=lambda x: x.published_parsed, reverse=True)

        items = []
        for entry in entries:
            title = entry.title.strip()
            link = entry.link.strip()
            if link not in SENT_LINKS:
                items.append((title, link))
                SENT_LINKS.add(link)
            else:
                print(f"略過（已發送）: {title}")

        if items:
            if "info.gov.hk" in url:
                # 純文字格式，含 ISD 登入頁
                body = "\n".join([f"{i+1}. {t}\n{l}" for i, (t, l) in enumerate(items)])
                message = ISD_LINK + "\n【政府新聞稿】\n" + body
                send_message(message, markdown=False)
            else:
                # MarkdownV2 格式
                body = "\n".join([f"{i+1}\. [{escape_md(t)}]({l})" for i, (t, l) in enumerate(items)])
                message = f"【{escape_md(name)}】\n" + body
                send_message(message, markdown=True)

    save_sent_links()

def send_alive_message():
    now = datetime.now(pytz.timezone("Asia/Hong_Kong"))
    if now.hour == 12 and now.minute == 0:
        send_message("我還活著，請放心\！")

while True:
    hk_time = datetime.now(pytz.timezone("Asia/Hong_Kong"))
    print("檢查時間：", hk_time.strftime("%H:%M"))
    if 8 <= hk_time.hour < 24 or (hk_time.hour == 0 and hk_time.minute <= 15):
        fetch_and_send()
        send_alive_message()
    time.sleep(60)
