import os
import time
import json
import requests
import feedparser
from datetime import datetime
import pytz
import re

# ================== 基本設定 ==================
BOT_TOKEN = os.environ.get("BOT_TOKEN")

# 支援多個 ID：優先讀 CHAT_IDS（逗號分隔），否則退回單一 CHAT_ID
_raw_ids = os.environ.get("CHAT_IDS", "").strip()
if _raw_ids:
    CHAT_IDS = [cid.strip() for cid in _raw_ids.split(",") if cid.strip()]
else:
    CHAT_IDS = []
    if os.environ.get("CHAT_ID"):
        CHAT_IDS.append(os.environ.get("CHAT_ID").strip())
CHAT_IDS = [cid for cid in dict.fromkeys(CHAT_IDS) if cid]  # 去重+過濾空值

# 檔案
SENT_FILE = "sent_urls_per_chat.json"
UPDATE_ID_FILE = "last_update_id.json"

# ================== HTML 轉義 ==================
def html_escape_text(s: str) -> str:
    if not s: return ""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def html_escape_attr(s: str) -> str:
    if not s: return ""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

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

# ================== 純文字降級工具 ==================
def strip_formatting_to_plain(s: str) -> str:
    if not s: return ""
    return re.sub(r"<[^>]+>", "", s)

# ================== 發送（逐人 + 解析錯誤自動降級） ==================
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

    # 1) HTML 嘗試
    payload = {
        "chat_id": chat_id,
        "text": html_text,
        "parse_mode": PRIMARY_PARSE_MODE,
        "disable_web_page_preview": disable_preview
    }
    status, ok, desc = _post(payload)

    # 2) 400 錯誤降級
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

# ================== 資料擷取邏輯 ==================
def fetch_feed_entries(source_label, rss_url):
    """
    統一回傳格式: list of tuples (raw_title, raw_link)
    """
    entries = []
    
    # --- 特別處理 HK01 (JSON API) ---
    if source_label == "HK01":
        try:
            headers = {"User-Agent": "Mozilla/5.0"}
            resp = requests.get(rss_url, headers=headers, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                items = data.get("items", [])
                for item in items[:15]: # 取前15條
                    try:
                        # HK01 V2 API 結構通常在 data 裡面
                        info = item.get("data", {})
                        title = info.get("title", "")
                        link = info.get("publishUrl", "")
                        # 補全相對連結
                        if link and not link.startswith("http"):
                            link = f"https://www.hk01.com{link}"
                        
                        if title and link:
                            entries.append((title, link))
                    except Exception:
                        continue
        except Exception as e:
            print(f"HK01 API 錯誤: {e}")
        return entries

    # --- 其他標準 RSS (Gov, RTHK, On.cc, MingPao, ST) ---
    try:
        # 明報或部分中文 RSS 可能有編碼問題，feedparser 通常能處理，但偶爾需強制
        feed = feedparser.parse(rss_url)
        
        # 簡單檢查是否抓取成功
        if not feed.entries and feed.bozo and "encoding" in str(feed.bozo_exception):
             # 極端情況：如果 feedparser 因編碼失敗，可嘗試用 requests decode 後再 parse (這裡先略過，通常 Railway 環境 OK)
             print(f"⚠️ RSS Encoding Warning: {source_label}")

        for entry in feed.entries[:15]:
            title = (getattr(entry, "title", "") or "").strip()
            link = (getattr(entry, "link", "") or "").strip()
            if title and link:
                entries.append((title, link))
    except Exception as e:
        print(f"RSS 解析失敗：{source_label} - {e}")
        
    return entries

# ================== 主邏輯：抓新聞並發送 ==================
def fetch_and_send():
    tz = pytz.timezone("Asia/Hong_Kong")
    print("🔍 正在檢查新聞…", datetime.now(tz).strftime("%H:%M:%S"))

    # 定義來源：(顯示名稱, URL, 模式[single|batch])
    # single: 每一條新聞發送一個通知 (適合政府重要公報)
    # batch:  累積成一張清單發送 (適合媒體新聞，避免洗版)
    sources = [
        # 政府新聞
        ("🏛 新聞稿", "https://www.info.gov.hk/gia/rss/general_zh.xml", "single"),
        
        # 媒體 (Batch 模式)
        ("📻 RTHK", "https://rthk.hk/rthk/news/rss/c_expressnews_clocal.xml", "batch"),
        ("💡 On.cc", "https://rsshub-production-9dfc.up.railway.app/oncc/zh-hant/news", "batch"),
        ("📰 HK01", "https://web-data.api.hk01.com/v2/feed/category/0", "batch"),
        ("🐯 星島", "https://www.stheadline.com/rss", "batch"),
        ("📝 明報", "https://news.mingpao.com/rss/ins/all.xml", "batch"),
    ]

    for label, url, mode in sources:
        print(f"📡 檢查中：{label}")
        items = fetch_feed_entries(label, url) # 取得標準化資料
        
        if not items:
            continue

        # --- 模式 A: Single (單條發送) ---
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

        # --- 模式 B: Batch (摘要清單) ---
        elif mode == "batch":
            for chat_id in CHAT_IDS:
                ensure_chat_key(SENT_MAP, chat_id)
                
                # 收集該用戶未讀的新聞
                unsent_items = []
                for raw_title, raw_link in items:
                    if raw_link in SENT_MAP[chat_id]:
                        continue
                    unsent_items.append((raw_title, raw_link))
                
                # 如果沒有新新聞，跳過該用戶
                if not unsent_items:
                    continue

                # 為了避免摘要過長，限制最多顯示 8 條 (多的忽略，避免 Telegram 限制)
                display_items = unsent_items[:8]

                # 組合訊息
                lines = []
                for i, (rt, rl) in enumerate(display_items, start=1):
                    # 轉義
                    t_esc = html_escape_text(rt)
                    l_esc = html_escape_attr(rl)
                    lines.append(f"{i}. <a href=\"{l_esc}\">{t_esc}</a>")
                
                header = f"<b>{label} 最新消息：</b>\n"
                html_msg = header + "\n".join(lines)
                
                ok = send_message_to(chat_id, html_msg, disable_preview=True)
                
                if ok:
                    # 將本次所有檢查到的連結 (含超過8條未顯示的) 都標記為已讀，避免下次重複跳出
                    for _, rl in unsent_items:
                        SENT_MAP[chat_id].add(rl)
                    save_sent_map(SENT_MAP)
                
                time.sleep(0.5) # Batch 之間稍微多停一點

# ================== /clear 指令 ==================
def check_clear_command():
    global LAST_UPDATE_ID, SENT_MAP
    if not BOT_TOKEN: return
    
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
        # 加上 offset 避免重複讀取舊訊息
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
                send_message_to(chat_id, "<b>🧹 已清空已發送紀錄 (Reset History)</b>", disable_preview=True)
                
    save_last_update_id(LAST_UPDATE_ID)

# ================== 主流程 ==================
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
            # 時間範圍：早上 08:30 ~ 凌晨 00:15
            is_active_time = (
                (hk_time.hour > 8 or (hk_time.hour == 8 and hk_time.minute >= 30)) and
                (hk_time.hour < 24 or (hk_time.hour == 0 and hk_time.minute <= 15))
            )
            
            if is_active_time:
                check_clear_command()
                fetch_and_send()
            
            # 報平安 (每日 12:00)
            if is_active_time and hk_time.strftime("%H:%M") == "12:00":
                send_message_all("<b>✅ System Alive</b>", disable_preview=True)
                time.sleep(60) # 避免重複發送
                
        except Exception as e:
            print(f"Main Loop Error: {e}")
            time.sleep(5)

        time.sleep(60)

if __name__ == "__main__":
    main_loop()
