import os
import time
import json
import re
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

# =============== MarkdownV2 轉義（關鍵） ===============
# 文字用的轉義（粗體文字、連結文字）
def md2_escape_text(s: str) -> str:
    if not s:
        return ""
    # 需要轉義的字元： _ * [ ] ( ) ~ ` > # + - = | { } . ! \
    return re.sub(r"([_\*$begin:math:display$$end:math:display$$begin:math:text$$end:math:text$~`>#+\-=|{}\.!\\])", r"\\\1", s)

# URL 用的轉義（常見是括號與反斜線）
def md2_escape_url(u: str) -> str:
    if not u:
        return ""
    return u.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")

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

# =============== 發送功能（MarkdownV2） ===============
def send_message_to(chat_id, text, disable_preview=False):
    if not BOT_TOKEN:
        print("❌ BOT_TOKEN 未設定，無法發送。")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "MarkdownV2",
        "disable_web_page_preview": disable_preview
    }
    try:
        r = requests.post(url, data=payload, timeout=15)
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
    for chat_id in CHAT_IDS:
        send_message_to(chat_id, text, disable_preview=disable_preview)

# =============== 抓新聞並發送（已套用轉義） ===============
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

            # 轉義後的文字/網址
            title = md2_escape_text(raw_title)
            link  = md2_escape_url(raw_link)

            if raw_link not in SENT_URLS:
                if "info.gov.hk" in rss_url:
                    msg = (
                        f"*{title}*\n"
                        f"[🔗 點此查看新聞]({link})\n\n"
                        f"👉 [GNMIS](https://www.isdnews.gov.hk/subscriber/loginpage)"
                    )
                    send_message(msg, disable_preview=False)

                elif "rthk.hk" in rss_url:
                    # 編號必須用 \\.
                    idx = len(rthk_lines) + 1
                    rthk_lines.append(f"{idx}\\. [{title}]({link})")

                SENT_URLS.add(raw_link)

        # RTHK 整批發送
        if "rthk.hk" in rss_url and rthk_lines:
            header = "*📻 RTHK 新聞摘要：*\n"
            full_message = header + "\n".join(rthk_lines)
            send_message(full_message, disable_preview=True)

    save_sent_urls()

    now = datetime.now(pytz.timezone("Asia/Hong_Kong"))
    if now.strftime("%H:%M") == "12:00":
        send_message(md2_escape_text("✅ 我還活著，請放心！"), disable_preview=True)

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

        message_obj = update.get("message") or {}
        text = (message_obj.get("text") or "").strip()
        chat = message_obj.get("chat") or {}
        chat_id = str(chat.get("id")) if chat.get("id") is not None else None

        if text == "/clear":
            if chat_id and chat_id in CHAT_IDS:
                SENT_URLS.clear()
                save_sent_urls()
                send_message_to(chat_id, md2_escape_text("🧹 已清空已發送紀錄"), disable_preview=True)
            else:
                if chat_id:
                    send_message_to(chat_id, md2_escape_text("⛔️ 此聊天不在授權清單，無法使用 /clear。"), disable_preview=True)

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
