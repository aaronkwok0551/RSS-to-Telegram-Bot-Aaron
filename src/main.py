import os
import time
import json
import requests
import feedparser
from datetime import datetime
import pytz

# =============== 基本設定 ===============
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

SENT_FILE = "sent_urls.json"
UPDATE_ID_FILE = "last_update_id.json"

# =============== HTML 轉義（關鍵） ===============
def html_escape(s: str) -> str:
    """把文字中的 & < > 轉義，避免 HTML 被破壞。"""
    if not s:
        return ""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def html_attr_escape(s: str) -> str:
    """用在 HTML 屬性（href）內，轉義 & < > 和雙引號。"""
    if not s:
        return ""
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
    )

# =============== 資料載入 ===============
try:
    with open(SENT_FILE, "r", encoding="utf-8") as f:
        SENT_URLS = set(json.load(f))
except Exception:
    SENT_URLS = set()

try:
    with open(UPDATE_ID_FILE, "r", encoding="utf-8") as f:
        LAST_UPDATE_ID = json.load(f)
except Exception:
    LAST_UPDATE_ID = 0

def save_sent_urls():
    try:
        with open(SENT_FILE, "w", encoding="utf-8") as f:
            json.dump(list(SENT_URLS), f, ensure_ascii=False)
    except Exception as e:
        print("save_sent_urls error:", e)

def save_update_id(update_id):
    try:
        with open(UPDATE_ID_FILE, "w", encoding="utf-8") as f:
            json.dump(update_id, f)
    except Exception as e:
        print("save_update_id error:", e)

# =============== 發送功能（HTML 解析） ===============
def send_message_to(chat_id, html_text, disable_preview=False):
    if not BOT_TOKEN:
        print("❌ BOT_TOKEN 未設定，無法發送。")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": html_text,
        "parse_mode": "HTML",                 # ← 使用 HTML，穩定不踩雷
        "disable_web_page_preview": disable_preview
    }
    try:
        r = requests.post(url, data=payload, timeout=15)
        try:
            j = r.json()
            ok = j.get("ok", False)
            desc = j.get("description", "")
        except Exception:
            ok = False
            desc = r.text[:300]
        if r.status_code != 200 or not ok:
            print(f"[sendMessage] to {chat_id}: status={r.status_code}, ok={ok}, desc={desc}")
    except Exception as e:
        print(f"發送錯誤 ({chat_id}): {e}")

def send_message(html_text, disable_preview=False):
    for chat_id in CHAT_IDS:
        send_message_to(chat_id, html_text, disable_preview=disable_preview)

# =============== 抓新聞並發送（已全面轉為 HTML） ===============
def fetch_and_send():
    print("🔍 正在檢查新聞…", datetime.now(pytz.timezone("Asia/Hong_Kong")).strftime("%H:%M:%S"))

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

        rthk_lines = []

        for entry in getattr(feed, "entries", [])[:10]:
            raw_title = (getattr(entry, "title", "") or "").strip()
            raw_link  = (getattr(entry, "link", "")  or "").strip()
            if not raw_title or not raw_link:
                continue

            # 轉義
            title = html_escape(raw_title)
            link  = html_attr_escape(raw_link)

            if raw_link not in SENT_URLS:
                if "info.gov.hk" in rss_url:
                    # 單則推送（粗體標題 + 可點連結）
                    msg = (
                        f"<b>{title}</b>\n"
                        f"<a href=\"{link}\">🔗 點此查看新聞</a>\n\n"
                        f"👉 <a href=\"https://www.isdnews.gov.hk/subscriber/loginpage\">GNMIS</a>"
                    )
                    send_message(msg, disable_preview=False)

                elif "rthk.hk" in rss_url:
                    idx = len(rthk_lines) + 1
                    rthk_lines.append(f"{idx}. <a href=\"{link}\">{title}</a>")

                SENT_URLS.add(raw_link)

        # RTHK 整批發送
        if "rthk.hk" in rss_url and rthk_lines:
            header = "<b>📻 RTHK 新聞摘要：</b>\n"
            full_message = header + "\n".join(rthk_lines)
            send_message(full_message, disable_preview=True)

    save_sent_urls()

    now = datetime.now(pytz.timezone("Asia/Hong_Kong"))
    if now.strftime("%H:%M") == "12:00":
        send_message("<b>✅ 我還活著，請放心！</b>", disable_preview=True)

# =============== 監聽 /clear 指令 ===============
def check_clear_command():
    global LAST_UPDATE_ID
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

    # 只處理簡單 /clear（可選）
        message_obj = update.get("message") or {}
        text = (message_obj.get("text") or "").strip()
        chat = message_obj.get("chat") or {}
        chat_id = str(chat.get("id")) if chat.get("id") is not None else None

        if text == "/clear":
            if chat_id and chat_id in CHAT_IDS:
                SENT_URLS.clear()
                save_sent_urls()
                send_message_to(chat_id, "<b>🧹 已清空已發送紀錄</b>", disable_preview=True)
            else:
                if chat_id:
                    send_message_to(chat_id, "⛔️ 此聊天不在授權清單，無法使用 /clear。", disable_preview=True)

        LAST_UPDATE_ID = update_id
        save_update_id(LAST_UPDATE_ID)

# =============== 主流程 ===============
def main_loop():
    if not BOT_TOKEN:
        print("❌ 未設定 BOT_TOKEN。請在 Railway 的 Variables 設定 BOT_TOKEN。")
        return
    if not CHAT_IDS:
        print("❌ 沒有任何接收 ID。請在 Railway 設定 CHAT_IDS（或 CHAT_ID）。")
        return

    print(f"✅ 已啟動，會發送到以下 ID：{', '.join(CHAT_IDS)}")

    while True:
        hk_time = datetime.now(pytz.timezone("Asia/Hong_Kong"))
        # 每天 08:30 ~ 24:00（含 00:15）
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
