- **設定運行時間：** 每天香港時間 08:30 至 00:15 運行 [oai_citation_attribution:0‡GitHub](https://github.com/Rongronggg9/RSS-to-Telegram-Bot?utm_source=chatgpt.com)

---

## 📄 `main.py` 程式碼

```python
import os
import time
import json
import requests
import feedparser
from datetime import datetime
import pytz

# Telegram Bot Token 和 Chat ID 從環境變數讀取
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")

# RSS 來源列表
RSS_URLS = [
  'https://www.info.gov.hk/gia/rss/general_zh.xml',
  'https://rthk.hk/rthk/news/rss/c_expressnews_clocal.xml'
]

# 已發送新聞的標題集合
SENT_TITLES_FILE = 'sent_titles.json'
try:
  with open(SENT_TITLES_FILE, 'r', encoding='utf-8') as f:
      SENT_TITLES = set(json.load(f))
except (FileNotFoundError, json.JSONDecodeError):
  SENT_TITLES = set()

def save_sent_titles():
  with open(SENT_TITLES_FILE, 'w', encoding='utf-8') as f:
      json.dump(list(SENT_TITLES), f, ensure_ascii=False)

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
          if title not in SENT_TITLES:
              msg = f"[新聞] {title}\n{link}"
              if "info.gov.hk" in rss:
                  msg += "\n更多詳情請見：https://www.isdnews.gov.hk/subscriber/loginpage"
              send_message(msg)
              SENT_TITLES.add(title)
  save_sent_titles()

  # 每天中午發送訊息
  if now.strftime("%H:%M") == "12:00":
      send_message("我還活著，請放心！")

while True:
  hk_time = datetime.now(pytz.timezone("Asia/Hong_Kong"))
  if (hk_time.hour > 8 or (hk_time.hour == 8 and hk_time.minute >= 30)) and (hk_time.hour < 24 or (hk_time.hour == 0 and hk_time.minute <= 15)):
      fetch_and_send()
  time.sleep(60)
