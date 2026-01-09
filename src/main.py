import os
import time
import json
import requests
import feedparser
from datetime import datetime
import pytz
import re
from urllib.parse import quote  # <--- [新增] 用來處理中文網址

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

# ================== HTML/URL 工具 ==================
def html_escape_text(s: str) -> str:
    if not s: return ""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def html_escape_attr(s: str) -> str:
    if not s: return ""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

def clean_url(url: str) -> str:
    """清理並對 URL 進行編碼，解決中文網址導致 Telegram 連結失效的問題"""
    if not url: return ""
    url = url.strip()
    # safe 參數指定不轉義的字符，保留 URL 結構符號
    return quote(url, safe=":/%?=&")

# ================== 載入/儲存 ==================
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
        print("save_sent_map error:", e)

def load_last_update_id():
    try:
        with open(UPDATE_ID_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return 0

def save_last_update_id(update_id):
    try:
        with open(UPDATE_ID_FILE, "w", encoding="utf-8") as f:
            json.dump(update_id, f)
    except Exception as e:
        print("save_update_id error:", e)

SENT_MAP = load_sent_map()
LAST_UPDATE_ID = load_last_update_id()

def ensure_chat_key(sent_map, chat_id):
    if chat_id not in sent_map:
        sent_map[chat_id] = set()

def strip_formatting_to_plain(s: str) -> str:
    if not s: return ""
    return re.sub(r"<[^>]+>", "", s)

# ================== 發送模組 ==================
PRIMARY_PARSE_MODE = "HTML"

def send_message_to(chat_id, html_text, disable_preview=False):
    if not BOT_TOKEN:
        print("❌ BOT_TOKEN 未設定")
        return False

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    def _post(payload):
        try:
            r = requests.post(url, data=payload, timeout=15)
            j = r.json()
            return r.status_code, j.get("ok", False), j.get("description", "")
        except Exception as e:
            return 0, False, str(e)

    payload = {
        "chat_id": chat_id,
        "text": html_text,
        "parse_mode": PRIMARY_PARSE_MODE,
        "disable_web_page_preview": disable_preview
    }
    status, ok, desc = _post(payload)

    if (not ok) and status == 400 and "parse entit" in (desc or "").lower():
        print(f"⚠️ [Format Error] {chat_id}: {desc}, switching to plain text.")
        fallback_payload = {
            "chat_id": chat_id,
            "text": strip_formatting_to_plain(html_text),
            "disable_web_page_preview": disable_preview
        }
        status2, ok2, desc2 = _post(fallback_payload)
        return ok2
    
    if not ok:
        print(f"❌ [Send Fail] {chat_id}: {desc}")
    
    return ok

def send_message_all(html_text, disable_preview=False):
    for chat_id in CHAT_IDS:
        send_message_to(chat_id, html_text, disable_preview=disable_preview)
        time.sleep(0.2)

# ================== 資料擷取 ==================
def fetch_feed_entries(source_label, rss_url):
    entries = []
    
    # --- HK01 ---
    if source_label == "📰 HK01":
        try:
            headers = {"User-Agent": "Mozilla/5.0"}
            resp = requests.get(rss_url, headers=headers, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                items = data.get("items", [])
                for item in items[:15]: 
                    try:
                        info = item.get("data", {})
                        title = info.get("title", "")
                        link = info.get("publishUrl", "")
                        if link and not link.startswith("http"):
                            link = f"https://www.hk01.com{link}"
                        
                        link = clean_url(link) # [修正] 編碼
                        
                        if title and link:
                            entries.append((title, link))
                    except Exception:
                        continue
        except Exception as e:
            print(f"HK01 API 錯誤: {e}")
        return entries

    # --- 標準 RSS ---
    try:
        feed = feedparser.parse(rss_url)
        
        for entry in feed.entries[:15]:
            title = (getattr(entry, "title", "") or "").strip()
            link = (getattr(entry, "link", "") or "").strip()
            
            # [修正] 明報若無 link，嘗試讀取 id
            if not link and "mingpao.com" in rss_url:
                link = (getattr(entry, "id", "") or "").strip()

            # [修正] 確保中文連結被編碼，否則 Telegram 會忽略 href
            link = clean_url(link)

            if title and link and link.startswith("http"):
                entries.append((title, link))

    except Exception as e:
        print(f"RSS 解析失敗：{source_label} - {e}")
        
    return entries

# ================== 主邏輯 ==================
def fetch_and_send():
    tz = pytz.timezone("Asia/Hong_Kong")
    print("🔍 正在檢查新聞…", datetime.now(tz).strftime("%H:%M:%S"))

    sources = [
        ("🏛 新聞稿", "https://www.info.gov.hk/gia/rss/general_zh.xml", "single"),
        ("📻 RTHK", "https://rthk.hk/rthk/news/rss/c_expressnews_clocal.xml", "batch"),
        ("💡 On.cc", "https://rsshub-production-9dfc.up.railway.app/oncc/zh-hant/news", "batch"),
        ("📰 HK01", "https://web-data.api.hk01.com/v2/feed/category/0", "batch"),
        ("🐯 星島", "https://www.stheadline.com/rss", "batch"),
        ("📝 明報", "https://news.mingpao.com/rss/ins/all.xml", "batch"),
    ]

    for label, url, mode in sources:
        print(f"📡 檢查中：{label}")
        items = fetch_feed_entries(label, url)
        
        if not items:
            continue

        if mode == "single":
            for raw_title, raw_link in items:
                title = html_escape_text(raw_title)
                link = html_escape_attr(raw_link)
                
                html_msg = (
                    f"<b>{title}</b>\n"
                    f"<a href=\"{link}\">🔗 點此查看新聞</a>\n\n"
                    f"👉 <a href=\"https://www.isdnews.gov.hk/subscriber/loginpage\">GNMIS</a>"
                )

                for chat_id in CHAT_IDS:
                    ensure_chat_key(SENT_MAP, chat_id)
                    if raw_link in SENT_MAP[chat_id]:
                        continue
                    
                    ok = send_message_to(chat_id, html_msg, disable_preview=False)
                    if ok:
                        SENT_MAP[chat_id].add(raw_link)
                        save_sent_map(SENT_MAP)
                    time.sleep(0.2)

        elif mode == "batch":
            for chat_id in CHAT_IDS:
                ensure_chat_key(SENT_MAP, chat_id)
                unsent_items = []
                for raw_title, raw_link in items:
                    if raw_link in SENT_MAP[chat_id]:
                        continue
                    unsent_items.append((raw_title, raw_link))
                
                if not unsent_items:
                    continue

                display_items = unsent_items[:8]

                lines = []
                for i, (rt, rl) in enumerate(display_items, start=1):
                    t_esc = html_escape_text(rt)
                    l_esc = html_escape_attr(rl)
                    lines.append(f"{i}. <a href=\"{l_esc}\">{t_esc}</a>")
                
                header = f"<b>{label} 最新消息：</b>\n"
                html_msg = header + "\n".join(lines)
                
                ok = send_message_to(chat_id, html_msg, disable_preview=True)
                
                if ok:
                    for _, rl in unsent_items:
                        SENT_MAP[chat_id].add(rl)
                    save_sent_map(SENT_MAP)
                
                time.sleep(0.5)

# ================== 清除指令 ==================
def check_clear_command():
    global LAST_UPDATE_ID, SENT_MAP
    if not BOT_TOKEN: return
    
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
        params = {"offset": LAST_UPDATE_ID + 1, "timeout": 10}
        resp = requests.get(url, params=params, timeout=20)
        data = resp.json()
    except Exception as e:
        print(f"Update Check Error: {e}")
        return

    if not data.get("ok"): return

    for update in data.get("result", []):
        update_id = update.get("update_id")
        LAST_UPDATE_ID = update_id
        
        message = update.get("message", {})
        text = message.get("text", "").strip()
        chat_id = str(message.get("chat", {}).get("id", ""))
        
        if text == "/clear":
            SENT_MAP = {}
            save_sent_map(SENT_MAP)
            if chat_id:
                send_message_to(chat_id, "<b>🧹 已清空已發送紀錄</b>", disable_preview=True)
                
    save_last_update_id(LAST_UPDATE_ID)

# ================== 啟動 ==================
def main_loop():
    if not BOT_TOKEN:
        print("❌ 未設定 BOT_TOKEN")
        return
    if not CHAT_IDS:
        print("❌ 未設定 CHAT_IDS")
        return

    print(f"✅ 監控啟動: {', '.join(CHAT_IDS)}")

    while True:
        try:
            hk_time = datetime.now(pytz.timezone("Asia/Hong_Kong"))
            is_active_time = (
                (hk_time.hour > 8 or (hk_time.hour == 8 and hk_time.minute >= 30)) and
                (hk_time.hour < 24 or (hk_time.hour == 0 and hk_time.minute <= 15))
            )
            
            if is_active_time:
                check_clear_command()
                fetch_and_send()
            
            if is_active_time and hk_time.strftime("%H:%M") == "12:00":
                send_message_all("<b>✅ System Alive</b>", disable_preview=True)
                time.sleep(60)
                
        except Exception as e:
            print(f"Main Loop Error: {e}")
            time.sleep(5)

        time.sleep(60)

if __name__ == "__main__":
    main_loop()
