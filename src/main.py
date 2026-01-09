import os
import time
import json
import requests
import feedparser
from datetime import datetime
import pytz
import re
from urllib.parse import quote

# ================== 基本設定 ==================
BOT_TOKEN = os.environ.get("BOT_TOKEN")

_raw_ids = os.environ.get("CHAT_IDS", "").strip()
if _raw_ids:
    CHAT_IDS = [cid.strip() for cid in _raw_ids.split(",") if cid.strip()]
else:
    CHAT_IDS = []
    if os.environ.get("CHAT_ID"):
        CHAT_IDS.append(os.environ.get("CHAT_ID").strip())
CHAT_IDS = [cid for cid in dict.fromkeys(CHAT_IDS) if cid]

SENT_FILE = "sent_urls_per_chat.json"
UPDATE_ID_FILE = "last_update_id.json"

# ================== 工具函數 ==================
def html_escape_text(s: str) -> str:
    if not s: return ""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def html_escape_attr(s: str) -> str:
    if not s: return ""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

def clean_url(url: str) -> str:
    """處理中文路徑網址，解決明報等連結失效問題"""
    if not url: return ""
    url = url.strip()
    return quote(url, safe=":/%?=&")

def strip_formatting_to_plain(s: str) -> str:
    if not s: return ""
    return re.sub(r"<[^>]+>", "", s)

# ================== 數據持久化 ==================
def load_sent_map():
    try:
        with open(SENT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return {str(k): set(v) for k, v in data.items()}
    except Exception: return {}

def save_sent_map(sent_map):
    try:
        serializable = {k: list(v) for k, v in sent_map.items()}
        with open(SENT_FILE, "w", encoding="utf-8") as f:
            json.dump(serializable, f, ensure_ascii=False)
    except Exception as e: print("Save Error:", e)

def load_last_update_id():
    try:
        with open(UPDATE_ID_FILE, "r", encoding="utf-8") as f: return json.load(f)
    except Exception: return 0

def save_last_update_id(update_id):
    try:
        with open(UPDATE_ID_FILE, "w", encoding="utf-8") as f: json.dump(update_id, f)
    except Exception: pass

SENT_MAP = load_sent_map()
LAST_UPDATE_ID = load_last_update_id()

def ensure_chat_key(sent_map, chat_id):
    if chat_id not in sent_map: sent_map[chat_id] = set()

# ================== Telegram 發送 ==================
def send_message_to(chat_id, html_text, disable_preview=False):
    if not BOT_TOKEN: return False
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": html_text,
        "parse_mode": "HTML",
        "disable_web_page_preview": disable_preview
    }
    try:
        r = requests.post(url, data=payload, timeout=15)
        if r.status_code == 400: # 格式錯誤降級
            payload["text"] = strip_formatting_to_plain(html_text)
            r = requests.post(url, data=payload, timeout=15)
        return r.json().get("ok", False)
    except: return False

# ================== 核心抓取邏輯 ==================
def fetch_feed_entries(source_label, rss_url):
    entries = []
    if source_label == "📰 HK01":
        try:
            resp = requests.get(rss_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
            if resp.status_code == 200:
                for item in resp.json().get("items", [])[:15]:
                    info = item.get("data", {})
                    title, link = info.get("title", ""), info.get("publishUrl", "")
                    if link and not link.startswith("http"): link = f"https://www.hk01.com{link}"
                    if title and link: entries.append((title, clean_url(link)))
        except: pass
    else:
        try:
            feed = feedparser.parse(rss_url)
            for entry in feed.entries[:15]:
                title = (getattr(entry, "title", "") or "").strip()
                link = (getattr(entry, "link", "") or getattr(entry, "id", "") or "").strip()
                link = clean_url(link)
                if title and link.startswith("http"): entries.append((title, link))
        except: pass
    return entries

# ================== 分流處理邏輯 ==================

def process_priority_news():
    """每分鐘處理：政府新聞稿 (單發) & RTHK (批次)"""
    sources = [
        ("🏛 新聞稿", "https://www.info.gov.hk/gia/rss/general_zh.xml", "single"),
        ("📻 RTHK", "https://rthk.hk/rthk/news/rss/c_expressnews_clocal.xml", "batch"),
    ]
    for label, url, mode in sources:
        items = fetch_feed_entries(label, url)
        if not items: continue

        for chat_id in CHAT_IDS:
            ensure_chat_key(SENT_MAP, chat_id)
            unsent = [it for it in items if it[1] not in SENT_MAP[chat_id]]
            if not unsent: continue

            if mode == "single":
                for rt, rl in unsent:
                    msg = f"<b>{html_escape_text(rt)}</b>\n<a href=\"{rl}\">🔗 查看新聞</a>\n\n👉 <a href=\"https://www.isdnews.gov.hk/subscriber/loginpage\">GNMIS</a>"
                    if send_message_to(chat_id, msg): SENT_MAP[chat_id].add(rl)
            else: # RTHK 批次
                lines = [f"{i}. <a href=\"{it[1]}\">{html_escape_text(it[0])}</a>" for i, it in enumerate(unsent[:8], 1)]
                if send_message_to(chat_id, f"<b>{label} 最新：</b>\n" + "\n".join(lines), True):
                    for it in unsent: SENT_MAP[chat_id].add(it[1])
            save_sent_map(SENT_MAP)

def process_grouped_news():
    """每 6 分鐘處理：合併所有商業媒體"""
    group_sources = [
        ("💡 On.cc", "https://rsshub-production-9dfc.up.railway.app/oncc/zh-hant/news"),
        ("📰 HK01", "https://web-data.api.hk01.com/v2/feed/category/0"),
        ("🐯 星島", "https://www.stheadline.com/rss"),
        ("📝 明報", "https://news.mingpao.com/rss/ins/all.xml"),
        ("🐯nowTV", "https://newsapi1.now.com/pccw-news-api/api/getNewsListv2?category=119&pageNo=1")
    ]
    
    fetched = {label: fetch_feed_entries(label, url) for label, url in group_sources}

    for chat_id in CHAT_IDS:
        ensure_chat_key(SENT_MAP, chat_id)
        sections = []
        all_new_links = []

        for label, items in fetched.items():
            unsent = [it for it in items if it[1] not in SENT_MAP[chat_id]]
            if unsent:
                lines = [f"<b>{label}</b>"] + [f"• <a href=\"{it[1]}\">{html_escape_text(it[0])}</a>" for it in unsent[:4]]
                sections.append("\n".join(lines))
                all_new_links.extend([it[1] for it in unsent])

        if sections:
            if send_message_to(chat_id, "<b>📰 綜合媒體快訊 (6分鐘彙整)</b>\n\n" + "\n\n".join(sections), True):
                for link in all_new_links: SENT_MAP[chat_id].add(link)
                save_sent_map(SENT_MAP)

# ================== 主循環 ==================
def main_loop():
    print(f"✅ 監控啟動，每分鐘檢查政府/RTHK，每6分鐘合併其他媒體。")
    loop_count = 0
    while True:
        try:
            hk_now = datetime.now(pytz.timezone("Asia/Hong_Kong"))
            if (hk_now.hour >= 8 and (hk_now.hour > 8 or hk_now.minute >= 30)) or (hk_now.hour == 0 and hk_now.minute <= 15):
                # 1. 優先 (1 min)
                process_priority_news()
                # 2. 合併 (6 min)
                if loop_count % 6 == 0:
                    process_grouped_news()
                
                # 指令檢查與報平安略過 (保持簡潔)
            loop_count += 1
        except Exception as e: print(f"Loop Error: {e}")
        time.sleep(60)

if __name__ == "__main__":
    main_loop()
