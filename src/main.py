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

# 去重 + 過濾空值
CHAT_IDS = [cid for cid in dict.fromkeys(CHAT_IDS) if cid]

# 檔案名稱
SENT_FILE = "sent_urls.json"
UPDATE_ID_FILE = "last_update_id.json"

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

# =============== 發送功能 ===============
def send_message_to(chat_id, text, disable_preview=False):
    """發送到單一 chat_id，並印出精簡結果（便於排錯、不會炸 log）。"""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": disable_preview
    }
    try:
        r = requests.post(url, data=payload, timeout=10)
        ok = False
        desc = ""
        try:
            j = r.json()
            ok = j.get("ok", False)
            desc = j.get("description", "")
        except Exception:
            desc = r.text[:300]
        if r.status_code != 200 or not ok:
            print(f"[sendMessage] to {chat_id}: status={r.status_code}, ok={ok}, desc={desc}")
    except Exception as e:
        print(f"發送錯誤 ({chat_id}): {e}")

def send_message(text, disable_preview=False):
    """發送給所有 CHAT_IDS。"""
    for chat_id in CHAT_IDS:
        send_message_to(chat_id, text, disable_preview=disable_preview)

# =============== 來源抓取 ===============
def fetch_and_send():
    print("🔍 正在檢查新聞…", datetime.now(pytz.timezone("Asia/Hong_Kong")).strftime("%H:%M:%S"))

    sources = [
        ("新聞稿", "https://www.info.gov.hk/gia/rss/general_zh.xml"),
        ("RTHK", "https://rthk.hk/rthk/news/rss/c_expressnews_clocal.xml"),
    ]

    for label, rss_url in sources:
        print(f"📡 檢查中：{label}")
        try:
            feed = feedparser.parse(rss_url)
        except Exception as e:
            print(f"RSS 解析失敗：{rss_url} - {e}")
            continue

        new_messages = []

        for entry in feed.entries[:10]:
            title = getattr(entry, "title", "").strip()
            link = getattr(entry, "link", "").strip()
            if not title or not link:
                continue

            if link not in SENT_URLS:
                if "info.gov.hk" in rss_url:
                    msg = (
                        f"*{title}*\n"
                        f"[🔗 點此查看新聞]({link})\n\n"
                        f"👉 [GNMIS](https://www.isdnews.gov.hk/subscriber/loginpage)"
                    )
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

# =============== 指令監聽 ===============
def check_clear_command():
    """監聽 /clear 指令：僅允許在授權的聊天使用。"""
    global LAST_UPDATE_ID
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    try:
        response = requests.get(url, timeout=15).json()
    except Exception as e:
        print(f"取得更新時發生錯誤: {e}")
        return

    for update in response.get("result", []):
        update_id = update.get("update_id")
        if update_id is None:
            continue
        if update_id <= LAST_UPDATE_ID:
            continue

        message_obj = update.get("message") or {}
        text = (message_obj.get("text") or "").strip()
        chat = message_obj.get("chat") or {}
        chat_id = str(chat.get("id")) if chat.get("id") is not None else None

        if text == "/clear":
            if chat_id and chat_id in CHAT_IDS:
                SENT_URLS.clear()
                save_sent_urls()
                send_message_to(chat_id, "🧹 已清空已發送紀錄", disable_preview=True)
            else:
                if chat_id:
                    send_message_to(chat_id, "⛔️ 此聊天不在授權清單，無法使用 /clear。", disable_preview=True)

        LAST_UPDATE_ID = update_id
        save_update_id(LAST_UPDATE_ID)

# =============== 安全 Debug 工具（可開關） ===============
def debug_print_updates(limit=3):
    """
    精簡列出 getUpdates 的關鍵欄位（避免炸 log）。
    使用方式：Railway 設定 DEBUG_UPDATES=1 後重啟，程式會列印少量資訊後結束。
    """
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    params = {
        "offset": int(LAST_UPDATE_ID) + 1 if isinstance(LAST_UPDATE_ID, int) else None,
        "limit": limit,
        "timeout": 0
    }
    params = {k: v for k, v in params.items() if v is not None}
    try:
        r = requests.get(url, params=params, timeout=10)
        j = r.json()
        results = j.get("result", [])
        print(f"[getUpdates] status={r.status_code}, count={len(results)}")
        for upd in results:
            chat = (upd.get("message") or {}).get("chat") or {}
            print(
                "update_id=", upd.get("update_id"),
                "| chat_id=", chat.get("id"),
                "| type=", chat.get("type"),
                "| title=", chat.get("title"),
                "| username=", chat.get("username")
            )
    except Exception as e:
        print("getUpdates error:", e)

# =============== 主流程 ===============
def main_loop():
    if not BOT_TOKEN:
        print("❌ 未設定 BOT_TOKEN。請在 Railway 的 Variables 設定 BOT_TOKEN。")
        return
    if not CHAT_IDS:
        print("❌ 沒有任何接收 ID。請在 Railway 設定 CHAT_IDS（或 CHAT_ID）。")
        return

    # ---- 一次性測試模式：僅在 TEST_SEND=1 時，單點發送給特定 ID 後結束 ----
    if os.environ.get("TEST_SEND") == "1":
        target = os.environ.get("TEST_TARGET", "") or (CHAT_IDS[0] if CHAT_IDS else "")
        if not target:
            print("TEST_SEND=1 但沒有可用的目標 ID。請設定 TEST_TARGET 或 CHAT_IDS。")
            return
        send_message_to(target, "🧪 單點測試：這是一條測試訊息（TEST_SEND=1）", disable_preview=True)
        return
    # ---- 精簡 getUpdates 模式：DEBUG_UPDATES=1 時只列印少量資訊後結束 ----
    if os.environ.get("DEBUG_UPDATES") == "1":
        print("🧪 DEBUG: 列出少量 getUpdates 資訊")
        debug_print_updates(limit=3)
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
