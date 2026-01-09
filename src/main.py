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

# ================== HTML/URL 工具 ==================
def html_escape_text(s: str) -> str:
    if not s: return ""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def html_escape_attr(s: str) -> str:
    if not s: return ""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

def clean_url(url: str) -> str:
    if not url: return ""
    url = url.strip()
    return quote(url, safe=":/%?=&")

def strip_formatting_to_plain(s: str) -> str:
    if not s: return ""
    return re.sub(r"<[^>]+>", "", s)

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

    # 失敗重試 (純文字)
    if (not ok) and status == 400 and "parse entit" in (desc or "").lower():
        print(f"⚠️ Format Error {chat_id}, retrying plain text.")
        payload["text"] = strip_formatting_to_plain(html_text)
        status2, ok2, desc2 = _post(payload)
        return ok2
    
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
                        link = clean_url(link)
                        if title and link:
                            entries.append((title, link))
                    except Exception:
                        continue
        except Exception:
            pass
        return entries

    # --- 標準 RSS ---
    try:
        feed = feedparser.parse(rss_url)
        for entry in feed.entries[:15]:
            title = (getattr(entry, "title", "") or "").strip()
            link = (getattr(entry, "link", "") or "").strip()
            
            if not link and "mingpao.com" in rss_url:
                link = (getattr(entry, "id", "") or "").strip()

            link = clean_url(link)

            if title and link and link.startswith("http"):
                entries.append((title, link))
    except Exception as e:
        print(f"RSS Fail: {source_label} - {e}")
        
    return entries

# ================== 邏輯 A: 每分鐘執行 (政府 + RTHK) ==================
def process_priority_news():
    print("⚡ [1-min] 檢查政府新聞稿 & RTHK...")
    
    # 定義優先來源
    sources = [
        ("🏛 新聞稿", "https://www.info.gov.hk/gia/rss/general_zh.xml", "single"),
        ("📻 RTHK", "https://rthk.hk/rthk/news/rss/c_expressnews_clocal.xml", "batch_separate"),
    ]

    for label, url, mode in sources:
        items = fetch_feed_entries(label, url)
        if not items: continue

        # 模式 1: 政府新聞稿 (單條即時)
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
                    if raw_link in SENT_MAP[chat_id]: continue
                    
                    if send_message_to(chat_id, html_msg, disable_preview=False):
                        SENT_MAP[chat_id].add(raw_link)
                        save_sent_map(SENT_MAP)
                    time.sleep(0.2)

        # 模式 2: RTHK (獨立 Batch)
        elif mode == "batch_separate":
            for chat_id in CHAT_IDS:
                ensure_chat_key(SENT_MAP, chat_id)
                unsent = [item for item in items if item[1] not in SENT_MAP[chat_id]]
                if not unsent: continue

                # 限制顯示數量
                display = unsent[:5]
                lines = []
                for i, (rt, rl) in enumerate(display, 1):
                    lines.append(f"{i}. <a href=\"{html_escape_attr(rl)}\">{html_escape_text(rt)}</a>")
                
                msg = f"<b>{label} 最新消息：</b>\n" + "\n".join(lines)
                
                if send_message_to(chat_id, msg, disable_preview=True):
                    for _, rl in unsent:
                        SENT_MAP[chat_id].add(rl)
                    save_sent_map(SENT_MAP)
                time.sleep(0.2)

# ================== 邏輯 B: 每3分鐘執行 (其他媒體群組) ==================
def process_grouped_news():
    print("📦 [3-min] 檢查商業媒體並合併發送...")

    group_sources = [
        ("💡 On.cc", "https://rsshub-production-9dfc.up.railway.app/oncc/zh-hant/news"),
        ("📰 HK01", "https://web-data.api.hk01.com/v2/feed/category/0"),
        ("🐯 星島", "https://www.stheadline.com/rss"),
        ("📝 明報", "https://news.mingpao.com/rss/ins/all.xml"),
    ]

    # 我們需要針對每個 User 建立客製化的「綜合包」
    # 因為每個 User 讀過的狀態可能不同 (雖然大多時候是一樣的)
    
    # 先把所有新聞抓下來，暫存記憶體，避免對每個 User 都重抓一次 RSS
    # 格式: {"On.cc": [(title, link), ...], "HK01": ...}
    fetched_data = {}
    for label, url in group_sources:
        fetched_data[label] = fetch_feed_entries(label, url)

    # 對每個 User 進行組裝
    for chat_id in CHAT_IDS:
        ensure_chat_key(SENT_MAP, chat_id)
        
        message_sections = [] # 存放各家媒體的文字段落
        pending_links = []    # 存放這次準備發送的所有連結 (用來更新 SENT_MAP)

        for label, items in fetched_data.items():
            # 篩選該 User 未讀的
            unsent = [item for item in items if item[1] not in SENT_MAP[chat_id]]
            
            if unsent:
                # 為了防止合併後太長，每家媒體最多只取前 4 條
                display = unsent[:4]
                
                lines = [f"<b>{label}</b>"] # 標題
                for i, (rt, rl) in enumerate(display, 1):
                    # 使用 • 作為列表符號，比較節省垂直空間
                    lines.append(f"• <a href=\"{html_escape_attr(rl)}\">{html_escape_text(rt)}</a>")
                
                message_sections.append("\n".join(lines))
                
                # 記錄所有未讀連結 (包含被隱藏的)，發送成功後標記為已讀
                for _, rl in unsent:
                    pending_links.append(rl)

        # 如果有任何內容，組合成一條大訊息發送
        if message_sections:
            header = "<b>📰 綜合媒體快訊</b>\n\n"
            full_msg = header + "\n\n".join(message_sections)
            
            # 發送
            if send_message_to(chat_id, full_msg, disable_preview=True):
                # 成功後，更新資料庫
                for link in pending_links:
                    SENT_MAP[chat_id].add(link)
                save_sent_map(SENT_MAP)
            
            time.sleep(0.5) # 避免觸發 Telegram 頻率限制

# ================== 清除指令 ==================
def check_clear_command():
    global LAST_UPDATE_ID, SENT_MAP
    if not BOT_TOKEN: return
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
        params = {"offset": LAST_UPDATE_ID + 1, "timeout": 5}
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()
    except Exception:
        return

    if not data.get("ok"): return

    for update in data.get("result", []):
        LAST_UPDATE_ID = update.get("update_id")
        text = update.get("message", {}).get("text", "").strip()
        chat_id = str(update.get("message", {}).get("chat", {}).get("id", ""))
        
        if text == "/clear":
            SENT_MAP = {}
            save_sent_map(SENT_MAP)
            if chat_id:
                send_message_to(chat_id, "<b>🧹 已清空紀錄</b>", disable_preview=True)
    
    save_last_update_id(LAST_UPDATE_ID)

# ================== 主流程 ==================
def main_loop():
    if not BOT_TOKEN or not CHAT_IDS:
        print("❌ 設定不完整")
        return

    print(f"✅ 監控啟動: {', '.join(CHAT_IDS)}")
    
    loop_counter = 0 # 計數器

    while True:
        try:
            hk_time = datetime.now(pytz.timezone("Asia/Hong_Kong"))
            is_active_time = (
                (hk_time.hour > 8 or (hk_time.hour == 8 and hk_time.minute >= 30)) and
                (hk_time.hour < 24 or (hk_time.hour == 0 and hk_time.minute <= 15))
            )
            
            if is_active_time:
                check_clear_command()
                
                # 1. 優先處理 (每分鐘跑)
                process_priority_news()

                # 2. 綜合處理 (每 3 分鐘跑一次: 0, 3, 6...)
                if loop_counter % 3 == 0:
                    process_grouped_news()
            
            # 每日報平安
            if is_active_time and hk_time.strftime("%H:%M") == "12:00" and loop_counter % 60 == 0:
                send_message_all("<b>✅ System Alive</b>", disable_preview=True)
                
        except Exception as e:
            print(f"Error: {e}")

        # 增加計數，睡眠 60 秒
        loop_counter += 1
        time.sleep(60)

if __name__ == "__main__":
    main_loop()
