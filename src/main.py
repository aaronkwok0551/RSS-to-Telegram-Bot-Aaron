import os
import time
import json
import requests
import feedparser
from datetime import datetime
import pytz

# === 設定區 ===
BOT_TOKEN = os.environ.get("BOT_TOKEN")

# 支援多個 ID：
# 1) 若有設定環境變數 CHAT_IDS（逗號分隔），會優先使用
# 2) 否則使用單一 CHAT_ID（若有）+ 你提供的 8499232968
_env_chat_ids = os.environ.get("CHAT_IDS", "").strip()
if _env_chat_ids:
    CHAT_IDS = [cid.strip() for cid in _env_chat_ids.split(",") if cid.strip()]
else:
    CHAT_IDS = []
    if os.environ.get("CHAT_ID"):
        CHAT_IDS.append(os.environ.get("CHAT_ID").strip())
    # 把你提供的新 ID 加入
    CHAT_IDS.append("8499232968")

# 去除可能的重複與空值
CHAT_IDS = [cid for cid in dict.fromkeys(CHAT_IDS) if cid]

# 檔名
SENT_FILE = "sent_urls.json"
UPDATE_ID_FILE = "last_update_id.json"

# === 讀取已發送紀錄 ===
try:
    with open(SENT_FILE, "r", encoding="utf-8") as f:
        SENT_URLS = set(json.load(f))
except:
    SENT_URLS = set()

# === 讀取最後處理過的 update_id ===
try:
    with open(UPDATE_ID_FILE, "r", encoding="utf-8") as f:
        LAST_UPDATE_ID = json.load(f)
except:
    LAST_UPDATE_ID = 0

def save_sent_urls():
    with open(SENT_FILE, "w", encoding="utf-8") as f:
        json.dump(list(SENT_URLS), f, ensure_ascii=False)

def save_update_id(update_id):
    with open(UPDATE_ID_FILE, "w", encoding="utf-8") as f:
        json.dump(update_id, f)

def send_message_to(chat_id, text, disable_preview=False):
    """只發送給指定 chat_id（用於回覆指令）。"""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": disable_preview
    }
    try:
        requests.post(url, data=payload, timeout=10)
    except Exception as e:
        print(f"發送錯誤 ({chat_id}): {e}")

def send_message(text, disable_preview=False):
    """發送給所有已設定的 CHAT_IDS。"""
    for chat_id in CHAT_IDS:
        send_message_to(chat_id, text, disable_preview=disable_preview)

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

def check_clear_command():
    """監聽 /clear 指令：清空已發送紀錄。只回覆觸發指令的聊天。"""
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
            continue  # 已處理過

        message_obj = update.get("message") or {}
        text = (message_obj.get("text") or "").strip()
        chat = message_obj.get("chat") or {}
        chat_id = str(chat.get("id")) if chat.get("id") is not None else None

        if text == "/clear":
            # 限制只有在 CHAT_IDS 清單內的聊天才能清除
            if chat_id and chat_id in CHAT_IDS:
                SENT_URLS.clear()
                save_sent_urls()
                send_message_to(chat_id, "🧹 已清空已發送紀錄", disable_preview=True)
            else:
                # 非授權聊天的指令：忽略或回覆提示
                if chat_id:
                    send_message_to(chat_id, "⛔️ 此聊天不在授權清單，無法使用 /clear。", disable_preview=True)

        LAST_UPDATE_ID = update_id
        save_update_id(LAST_UPDATE_ID)

def main_loop():
    if not BOT_TOKEN:
        print("❌ 未設定 BOT_TOKEN 環境變數。請先設定。")
        return
    if not CHAT_IDS:
        print("❌ 沒有可用的 CHAT_ID。請設定 CHAT_ID 或 CHAT_IDS。")
        return

    print(f"✅ 已啟動，將會發送到以下聊天 ID：{', '.join(CHAT_IDS)}")

    while True:
        hk_time = datetime.now(pytz.timezone("Asia/Hong_Kong"))
        # 08:30 ~ 24:00（含 00:15）
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
