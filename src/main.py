import os
import time
import json
import requests
import feedparser
from datetime import datetime
import pytz
import re
from urllib.parse import quote

# ================== 1. 基本設定 ==================
BOT_TOKEN = os.environ.get("BOT_TOKEN")

# 處理 Chat IDs (支援多個 ID，以逗號分隔)
_raw_ids = os.environ.get("CHAT_IDS", "").strip()
CHAT_IDS = []
if _raw_ids:
    CHAT_IDS = [cid.strip() for cid in _raw_ids.split(",") if cid.strip()]
else:
    # 兼容舊變數名
    if os.environ.get("CHAT_ID"):
        CHAT_IDS.append(os.environ.get("CHAT_ID").strip())
CHAT_IDS = [cid for cid in dict.fromkeys(CHAT_IDS) if cid] # 去重

SENT_FILE = "sent_urls_per_chat.json"
UPDATE_ID_FILE = "last_update_id.json"

# ================== 2. 工具函數 ==================
def html_escape_text(s: str) -> str:
    if not s: return ""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def clean_url(url: str) -> str:
    """處理中文路徑網址，解決明報等連結失效問題"""
    if not url: return ""
    url = url.strip()
    return quote(url, safe=":/%?=&")

def strip_formatting_to_plain(s: str) -> str:
    if not s: return ""
    return re.sub(r"<[^>]+>", "", s)

# ================== 3. 數據持久化 (讀寫記錄) ==================
def load_sent_map():
    try:
        with open(SENT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return {str(k): set(v) for k, v in data.items()}
    except Exception:
        return {}

def save_sent_map(sent_map):
    try:
        serializable = {k: list(v) for k, v in sent_map.items()}
        with open(SENT_FILE, "w", encoding="utf-8") as f:
            json.dump(serializable, f, ensure_ascii=False)
    except Exception as e:
        print(f"Save Error: {e}")

SENT_MAP = load_sent_map()

def ensure_chat_key(sent_map, chat_id):
    if chat_id not in sent_map:
        sent_map[chat_id] = set()

# ================== 4. Telegram 發送模組 ==================
def send_message_to(chat_id, html_text, disable_preview=False):
    if not BOT_TOKEN:
        print("❌ 錯誤: 未設定 BOT_TOKEN")
        return False
        
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": html_text,
        "parse_mode": "HTML",
        "disable_web_page_preview": disable_preview
    }
    
    try:
        r = requests.post(url, data=payload, timeout=15)
        # 如果 HTML 格式錯誤 (例如未閉合標籤)，嘗試降級為純文字發送
        if r.status_code == 400: 
            print(f"⚠️ 格式錯誤，嘗試純文字重發: {chat_id}")
            payload["text"] = strip_formatting_to_plain(html_text)
            payload.pop("parse_mode", None)
            r = requests.post(url, data=payload, timeout=15)
            
        return r.json().get("ok", False)
    except Exception as e:
        print(f"Send Error: {e}")
        return False

# ================== 5. 核心抓取邏輯 (已修復 NowTV) ==================
def fetch_feed_entries(source_label, rss_url):
    entries = []
    
    # --- 分支 A: HK01 (JSON API) ---
    if source_label == "📰 HK01":
        try:
            resp = requests.get(rss_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
            if resp.status_code == 200:
                for item in resp.json().get("items", [])[:15]:
                    info = item.get("data", {})
                    title = info.get("title", "")
                    link = info.get("publishUrl", "")
                    if link and not link.startswith("http"):
                        link = f"https://www.hk01.com{link}"
                    if title and link:
                        entries.append((title, clean_url(link)))
        except Exception as e:
            print(f"HK01 Error: {e}")

    # --- 分支 B: NowTV (JSON API - 已修復) ---
    elif source_label == "🐯nowTV":
        try:
            resp = requests.get(rss_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list): # Now API 返回 List
                    for item in data[:15]:
                        title = item.get("title", "")
                        news_id = item.get("newsId", "")
                        if title and news_id:
                            # 拼湊網頁連結
                            link = f"https://news.now.com/home/local/player?newsId={news_id}"
                            entries.append((title, clean_url(link)))
        except Exception as e:
            print(f"NowTV Error: {e}")

    # --- 分支 C: 標準 RSS/XML (RTHK, 明報, 星島, On.cc) ---
    else:
        try:
            feed = feedparser.parse(rss_url)
            for entry in feed.entries[:15]:
                title = (getattr(entry, "title", "") or "").strip()
                link = (getattr(entry, "link", "") or getattr(entry, "id", "") or "").strip()
                # 部分 RSS 可能帶有空格或特殊字符
                link = clean_url(link)
                if title and link.startswith("http"):
                    entries.append((title, link))
        except Exception as e:
            print(f"RSS Error ({source_label}): {e}")
            
    return entries

# ================== 6. 業務邏輯: 優先處理 & 彙整處理 ==================

def process_priority_news():
    """每分鐘執行：政府新聞 (單則發送) & RTHK (批次發送)"""
    sources = [
        ("🏛 新聞稿", "https://www.info.gov.hk/gia/rss/general_zh.xml", "single"),
        ("📻 RTHK", "https://rthk.hk/rthk/news/rss/c_expressnews_clocal.xml", "batch"),
    ]
    
    for label, url, mode in sources:
        items = fetch_feed_entries(label, url)
        if not items: continue

        for chat_id in CHAT_IDS:
            ensure_chat_key(SENT_MAP, chat_id)
            # 過濾已發送
            unsent = [it for it in items if it[1] not in SENT_MAP[chat_id]]
            if not unsent: continue

            # 模式 A: 單則發送 (政府新聞)
            if mode == "single":
                for rt, rl in unsent:
                    msg = (f"<b>{html_escape_text(rt)}</b>\n"
                           f"<a href=\"{rl}\">🔗 查看新聞</a>\n\n"
                           f"👉 <a href=\"https://www.isdnews.gov.hk/subscriber/loginpage\">GNMIS</a>")
                    if send_message_to(chat_id, msg):
                        SENT_MAP[chat_id].add(rl)
            
            # 模式 B: 批次發送 (RTHK)
            else:
                lines = [f"{i}. <a href=\"{it[1]}\">{html_escape_text(it[0])}</a>" 
                         for i, it in enumerate(unsent[:8], 1)]
                msg = f"<b>{label} 最新：</b>\n" + "\n".join(lines)
                if send_message_to(chat_id, msg, disable_preview=True):
                    for it in unsent:
                        SENT_MAP[chat_id].add(it[1])
            
            save_sent_map(SENT_MAP)

def process_grouped_news():
    """每 6 分鐘執行：合併所有商業媒體 (On.cc, HK01, 星島, 明報, NowTV)"""
    group_sources = [
        ("💡 On.cc", "https://rsshub-production-9dfc.up.railway.app/oncc/zh-hant/news"),
        ("📰 HK01", "https://web-data.api.hk01.com/v2/feed/category/0"),
        ("🐯 星島", "https://www.stheadline.com/rss"),
        ("📝 明報", "https://news.mingpao.com/rss/ins/all.xml"),
        ("🐯nowTV", "https://newsapi1.now.com/pccw-news-api/api/getNewsListv2?category=119&pageNo=1")
    ]
    
    # 抓取所有來源
    fetched = {}
    for label, url in group_sources:
        fetched[label] = fetch_feed_entries(label, url)

    for chat_id in CHAT_IDS:
        ensure_chat_key(SENT_MAP, chat_id)
        sections = []
        all_new_links = []

        for label, items in fetched.items():
            unsent = [it for it in items if it[1] not in SENT_MAP[chat_id]]
            if unsent:
                # 每個來源最多取前 4 則
                lines = [f"<b>{label}</b>"] + \
                        [f"• <a href=\"{it[1]}\">{html_escape_text(it[0])}</a>" for it in unsent[:4]]
                sections.append("\n".join(lines))
                all_new_links.extend([it[1] for it in unsent])

        if sections:
            header = "<b>📰 綜合媒體快訊 (彙整)</b>\n\n"
            full_msg = header + "\n\n".join(sections)
            
            if send_message_to(chat_id, full_msg, disable_preview=True):
                for link in all_new_links:
                    SENT_MAP[chat_id].add(link)
                save_sent_map(SENT_MAP)

# ================== 7. 主循環 (Main Loop) ==================
def main_loop():
    print(f"✅ News Bot 啟動成功")
    print(f"✅ 監控名單: {len(CHAT_IDS)} 個 Chats")
    
    loop_count = 0
    while True:
        try:
            hk_now = datetime.now(pytz.timezone("Asia/Hong_Kong"))
            
            # === 時間過濾 (睡眠模式) ===
            # 規則: 
            # 1. 08:00 - 23:59 (正常運作)
            # 2. 00:00 - 00:15 (午夜最後一更)
            is_active_time = (
                (hk_now.hour >= 8 and (hk_now.hour > 8 or hk_now.minute >= 30)) or 
                (hk_now.hour == 0 and hk_now.minute <= 15)
            )

            if is_active_time:
                # 1. 優先新聞 (每分鐘)
                process_priority_news()

                # 2. 綜合新聞 (每 6 分鐘)
                if loop_count % 6 == 0:
                    process_grouped_news()
            else:
                if loop_count % 60 == 0:
                    print(f"💤 睡眠時間... ({hk_now.strftime('%H:%M')})")

            loop_count += 1
            
        except Exception as e:
            print(f"❌ Main Loop Exception: {e}")
            
        # 休息 60 秒
        time.sleep(60)

if __name__ == "__main__":
    main_loop()
