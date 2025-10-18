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
SENT_FILE = "sent_urls_per_chat.json"   # << 改成每個 chat 獨立去重
UPDATE_ID_FILE = "last_update_id.json"

# ================== HTML 轉義 ==================
def html_escape_text(s: str) -> str:
    if not s:
        return ""
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;"))

def html_escape_attr(s: str) -> str:
    if not s:
        return ""
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;"))

# ================== 載入/儲存 ==================
# 結構：{"264588454": ["url1","url2",...], "8499232968": ["url3",...]}
def load_sent_map():
    try:
        with open(SENT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            # 轉成 set，便於判斷
            return {str(k): set(v) for k, v in data.items()}
    except Exception:
        return {}

def save_sent_map(sent_map):
    try:
        # 轉回 list 儲存
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
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", "", s)  # 去 HTML 標籤
    return s

# ================== 發送（逐人 + 解析錯誤自動降級） ==================
PRIMARY_PARSE_MODE = "HTML"  # HTML 最穩：粗體+可點連結

def send_message_to(chat_id, html_text, disable_preview=False):
    if not BOT_TOKEN:
        print("❌ BOT_TOKEN 未設定")
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    def _post(payload):
        r = requests.post(url, data=payload, timeout=15)
        try:
            j = r.json()
            return r.status_code, j.get("ok", False), j.get("description", "")
        except Exception:
            return r.status_code, False, r.text[:300]

    # 1) 以 HTML 嘗試
    payload = {
        "chat_id": chat_id,
        "text": html_text,
        "parse_mode": PRIMARY_PARSE_MODE,
        "disable_web_page_preview": disable_preview
    }
    status, ok, desc = _post(payload)

    # 2) 若 400（解析錯）→ 純文字補發一次
    if (not ok) and status == 400 and "parse entit" in (desc or "").lower():
        fallback_payload = {
            "chat_id": chat_id,
            "text": strip_formatting_to_plain(html_text),
            "disable_web_page_preview": disable_preview
        }
        status2, ok2, desc2 = _post(fallback_payload)
        print(f"[fallback->plain] to {chat_id}: status={status2}, ok={ok2}, desc={desc2}")
        if not ok2:
            print(f"[primary failed] to {chat_id}: status={status}, ok={ok}, desc={desc}")
        return ok2
    else:
        if not ok:
            print(f"[sendMessage] to {chat_id}: status={status}, ok={ok}, desc={desc}")
        return ok

def send_message_all(html_text, disable_preview=False):
    # 逐人發送，彼此間隔 0.2 秒，避免節流
    results = {}
    for chat_id in CHAT_IDS:
        ok = send_message_to(chat_id, html_text, disable_preview=disable_preview)
        results[chat_id] = ok
        time.sleep(0.2)
    return results

# ================== 抓新聞並發送 ==================
def fetch_and_send():
    tz = pytz.timezone("Asia/Hong_Kong")
    print("🔍 正在檢查新聞…", datetime.now(tz).strftime("%H:%M:%S"))

    sources = [
        ("新聞稿", "https://www.info.gov.hk/gia/rss/general_zh.xml"),
        ("RTHK",  "https://rthk.hk/rthk/news/rss/c_expressnews_clocal.xml"),
    ]

    for label, rss_url in sources:
        print(f"📡 檢查中：{label}")
        try:
            feed = feedparser.parse(rss_url)
        except Exception as e:
            print(f"RSS 解析失敗：{rss_url} - {e}")
            continue

        # RTHK 批次
        rthk_items = []

        for entry in getattr(feed, "entries", [])[:10]:
            raw_title = (getattr(entry, "title", "") or "").strip()
            raw_link  = (getattr(entry, "link", "")  or "").strip()
            if not raw_title or not raw_link:
                continue

            # HTML 轉義
            title = html_escape_text(raw_title)
            link  = html_escape_attr(raw_link)

            # --- info.gov.hk：單則發送 ---
            if "info.gov.hk" in rss_url:
                # 先組訊息
                html_msg = (
                    f"<b>{title}</b>\n"
                    f"<a href=\"{link}\">🔗 點此查看新聞</a>\n\n"
                    f"👉 <a href=\"https://www.isdnews.gov.hk/subscriber/loginpage\">GNMIS</a>"
                )

                # 對每個 chat_id 檢查是否已發過（逐人去重）
                for chat_id in CHAT_IDS:
                    ensure_chat_key(SENT_MAP, chat_id)
                    if raw_link in SENT_MAP[chat_id]:
                        continue  # 這個人已經發送過這條，跳過

                    ok = send_message_to(chat_id, html_msg, disable_preview=False)
                    if ok:
                        SENT_MAP[chat_id].add(raw_link)
                        save_sent_map(SENT_MAP)
                    time.sleep(0.2)

            # --- RTHK：先收集，稍後批次發 ---
            elif "rthk.hk" in rss_url:
                rthk_items.append((raw_title, raw_link, title, link))

        # RTHK 批次發送（每人各送一次；各自去重）
        if rthk_items:
            for chat_id in CHAT_IDS:
                ensure_chat_key(SENT_MAP, chat_id)

                # 篩掉這個 chat 已經發過的連結，只把新的列進清單
                lines = []
                for i, (raw_title, raw_link, title, link) in enumerate(rthk_items, start=1):
                    if raw_link in SENT_MAP[chat_id]:
                        continue
                    # 用序號+超連結
                    lines.append(f"{i}. <a href=\"{link}\">{title}</a>")

                if lines:
                    header = "<b>📻 RTHK 新聞摘要：</b>\n"
                    html_msg = header + "\n".join(lines)

                    ok = send_message_to(chat_id, html_msg, disable_preview=True)
                    if ok:
                        # 把此次發出的每一條都記錄到這個 chat 的已發清單
                        for _, raw_link, _, _ in rthk_items:
                            SENT_MAP[chat_id].add(raw_link)
                        save_sent_map(SENT_MAP)
                    time.sleep(0.2)

    # 每天 12:00 報平安
    now = datetime.now(pytz.timezone("Asia/Hong_Kong"))
    if now.strftime("%H:%M") == "12:00":
        send_message_all("<b>✅ 我還活著，請放心！</b>", disable_preview=True)

# ================== /clear 指令 ==================
def check_clear_command():
    global LAST_UPDATE_ID, SENT_MAP
    if not BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    try:
        response = requests.get(url, timeout=20).json()
    except Exception as e:
        print(f"取得更新時發生錯誤: {e}")
        return

    for update in response.get("result", []):
        update_id = update.get("update_id")
        if update_id is None or (isinstance(LAST_UPDATE_ID, int) and update_id <= LAST_UPDATE_ID):
            continue

        message_obj = update.get("message") or {}
        text = (message_obj.get("text") or "").strip()
        chat = message_obj.get("chat") or {}
        chat_id = str(chat.get("id")) if chat.get("id") is not None else None

        if text == "/clear":
            # 清掉所有 chat 的發送紀錄
            SENT_MAP = {}
            save_sent_map(SENT_MAP)
            send_message_to(chat_id, "<b>🧹 已清空已發送紀錄</b>", disable_preview=True)

        LAST_UPDATE_ID = update_id
        save_last_update_id(LAST_UPDATE_ID)

# ================== 主流程 ==================
def main_loop():
    if not BOT_TOKEN:
        print("❌ 未設定 BOT_TOKEN。")
        return
    if not CHAT_IDS:
        print("❌ 沒有任何接收 ID。請在 Railway 設定 CHAT_IDS（或 CHAT_ID）。")
        return

    print(f"✅ 已啟動，會發送到以下 ID：{', '.join(CHAT_IDS)}")

    while True:
        hk_time = datetime.now(pytz.timezone("Asia/Hong_Kong"))
        within_mins = (
            (hk_time.hour > 8 or (hk_time.hour == 8 and hk_time.minute >= 30)) and
            (hk_time.hour < 24 or (hk_time.hour == 0 and hk_time.minute <= 15))
        )
        if within_mins:
            check_clear_command()
            fetch_and_send()
        time.sleep(60)

if __name__ == "__main__":
    main_loop()
