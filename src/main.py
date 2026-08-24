# -*- coding: utf-8 -*-
"""
Telegram 新聞 Bot（管理員審批訂閱版）

訂閱流程：
1. 新用戶先私訊 Bot，輸入 /subscribe。
2. Bot 把申請通知 ADMIN_CHAT_ID，附上「批准／拒絕」按鈕。
3. 只有管理員批准後，用戶才會加入新聞發送名單。
4. 用戶可隨時輸入 /unsubscribe 取消訂閱。

Railway 只需一次設定：
- BOT_TOKEN：Telegram Bot Token
- ADMIN_CHAT_ID：管理員的 Telegram numeric chat ID
- DATA_DIR：建議設為 /app/data，並把 Railway Volume 掛載到 /app/data

向下兼容：
- 如未設定 ADMIN_CHAT_ID，會使用 CHAT_IDS 的第一個 ID 作管理員。
- 原有 CHAT_IDS / CHAT_ID 會在首次啟動時自動加入已批准訂閱者。
"""

import html
import json
import os
import re
import time
import urllib3
from datetime import datetime
from urllib.parse import quote

import feedparser
import pytz
import requests


# ================== 0. 系統優化 ==================
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
HTTP = requests.Session()


# ================== 1. 基本設定 ==================
BOT_TOKEN = (os.environ.get("BOT_TOKEN") or "").strip()

_raw_ids = os.environ.get("CHAT_IDS", "").strip()
INITIAL_CHAT_IDS = []
if _raw_ids:
    INITIAL_CHAT_IDS = [cid.strip() for cid in _raw_ids.split(",") if cid.strip()]
elif os.environ.get("CHAT_ID"):
    INITIAL_CHAT_IDS.append(os.environ.get("CHAT_ID", "").strip())
INITIAL_CHAT_IDS = list(dict.fromkeys(cid for cid in INITIAL_CHAT_IDS if cid))

# Railway 的主要環境變數使用 ADMIN_ID。
# 同時兼容舊名稱 ADMIN_CHAT_ID，避免現有部署設定失效。
ADMIN_ID = (
    os.environ.get("ADMIN_ID")
    or os.environ.get("ADMIN_CHAT_ID")
    or ""
).strip()

if not ADMIN_ID and INITIAL_CHAT_IDS:
    ADMIN_ID = INITIAL_CHAT_IDS[0]

# 程式其他部分原本使用 ADMIN_CHAT_ID；保留別名以確保全部功能正常。
ADMIN_CHAT_ID = ADMIN_ID

# Railway 建議把 Volume 掛載到 /app/data，然後設 DATA_DIR=/app/data。
DATA_DIR = (os.environ.get("DATA_DIR") or ".").strip()
os.makedirs(DATA_DIR, exist_ok=True)

STATE_FILE = os.path.join(DATA_DIR, "subscriber_state.json")
SENT_FILE = os.path.join(DATA_DIR, "sent_urls_per_chat.json")
MAX_SENT_CACHE = 500

# Telegram polling 頻率；新聞抓取仍維持每 1 分鐘／6 分鐘。
TELEGRAM_POLL_INTERVAL = 3
PRIORITY_NEWS_INTERVAL = 60
GROUPED_NEWS_INTERVAL = 360

# 健康監控追蹤器
FEED_ERRORS = {}
DEFAULT_ALERT_THRESHOLD = 10
CUSTOM_THRESHOLDS = {
    "📜 商報評論": 50,
    "🔵 點新聞評論": 30,
    "🔵 文匯評論": 30,
    "📝 明報": 15,
}


# ================== 2. 通用工具函數 ==================
def html_escape_text(value: str) -> str:
    if not value:
        return ""
    value = html.unescape(str(value))
    return html.escape(value, quote=True)


def clean_title_simple(value: str) -> str:
    if not value:
        return ""
    value = re.sub(r"<[^>]+>", "", str(value))
    value = html.unescape(value)
    value = re.sub(r"\s*\d+(分鐘|小時|天)前.*", "", value)
    value = value.replace("分享", "")
    value = re.sub(r"\d{4}-\d{2}-\d{2}", "", value)
    value = re.sub(r"\s*\d{1,2}:\d{2}$", "", value.strip())
    return value.strip()


def clean_url(url: str) -> str:
    if not url:
        return ""
    url = url.strip()
    if "hkej.com" in url:
        url = url.replace("m.hkej.com", "www.hkej.com").replace("++", "")
    safe_url = quote(url, safe=":/%?=&")
    return html.escape(safe_url, quote=True)


def get_unique_id(title: str, url: str, pub_time: str) -> str:
    """標題、URL 與發佈時間合併的防重複憑證。"""
    return f"{title}|{url}|{pub_time}"


def utc_timestamp() -> int:
    return int(time.time())


def display_name_from_user(user: dict) -> str:
    first_name = str(user.get("first_name", "")).strip()
    last_name = str(user.get("last_name", "")).strip()
    full_name = " ".join(part for part in (first_name, last_name) if part).strip()
    return full_name or "未提供名稱"


def normalize_chat_id(value) -> str:
    return str(value).strip()


# ================== 3. 訂閱與發送紀錄持久化 ==================
def default_state() -> dict:
    return {
        "subscribers": {},
        "pending": {},
        "last_update_id": 0,
    }


def load_state() -> dict:
    state = default_state()
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as file:
                loaded = json.load(file)
            if isinstance(loaded, dict):
                state["subscribers"] = loaded.get("subscribers", {}) or {}
                state["pending"] = loaded.get("pending", {}) or {}
                state["last_update_id"] = int(loaded.get("last_update_id", 0) or 0)
    except Exception as exc:
        print(f"⚠️ 讀取訂閱狀態失敗：{exc}")

    # 將 Railway 原有 CHAT_IDS / CHAT_ID 種入已批准名單，方便無痛升級。
    for chat_id in INITIAL_CHAT_IDS:
        state["subscribers"].setdefault(
            normalize_chat_id(chat_id),
            {
                "name": "Railway 初始訂閱者",
                "username": "",
                "approved_at": utc_timestamp(),
                "approved_by": "environment",
            },
        )
    return state


def save_state() -> None:
    temp_file = f"{STATE_FILE}.tmp"
    try:
        with open(temp_file, "w", encoding="utf-8") as file:
            json.dump(BOT_STATE, file, ensure_ascii=False, indent=2)
        os.replace(temp_file, STATE_FILE)
    except Exception as exc:
        print(f"❌ 儲存訂閱狀態失敗：{exc}")


def load_sent_map() -> dict:
    try:
        if os.path.exists(SENT_FILE):
            with open(SENT_FILE, "r", encoding="utf-8") as file:
                data = json.load(file)
            return {str(key): set(value) for key, value in data.items()}
    except Exception as exc:
        print(f"⚠️ 讀取新聞發送紀錄失敗：{exc}")
    return {}


def save_sent_map(sent_map: dict) -> None:
    temp_file = f"{SENT_FILE}.tmp"
    try:
        serializable = {}
        for key, values in sent_map.items():
            value_list = list(values)
            if len(value_list) > MAX_SENT_CACHE:
                value_list = value_list[-MAX_SENT_CACHE:]
            serializable[str(key)] = value_list
        with open(temp_file, "w", encoding="utf-8") as file:
            json.dump(serializable, file, ensure_ascii=False)
        os.replace(temp_file, SENT_FILE)
    except Exception as exc:
        print(f"❌ 儲存新聞發送紀錄失敗：{exc}")


def get_approved_chat_ids() -> list[str]:
    return list(BOT_STATE["subscribers"].keys())


def ensure_chat_key(sent_map: dict, chat_id: str) -> None:
    if chat_id not in sent_map:
        sent_map[chat_id] = set()


BOT_STATE = load_state()
SENT_MAP = load_sent_map()
save_state()


# ================== 4. Telegram API ==================
def telegram_api(method: str, payload: dict | None = None, timeout: int = 20):
    if not BOT_TOKEN:
        print("❌ 尚未設定 BOT_TOKEN")
        return None

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    try:
        response = HTTP.post(url, data=payload or {}, timeout=timeout)
        result = response.json()
        if response.status_code != 200 or not result.get("ok"):
            print(
                f"❌ Telegram API {method} 失敗："
                f"HTTP {response.status_code}, {response.text}"
            )
            return None
        return result.get("result")
    except Exception as exc:
        print(f"❌ Telegram API {method} 例外：{exc}")
        return None


def send_message_to(
    chat_id: str,
    html_text: str,
    disable_preview: bool = False,
    reply_markup: dict | None = None,
) -> bool:
    payload = {
        "chat_id": normalize_chat_id(chat_id),
        "text": html_text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true" if disable_preview else "false",
    }
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    return telegram_api("sendMessage", payload) is not None


def answer_callback_query(callback_query_id: str, text: str = "") -> None:
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    telegram_api("answerCallbackQuery", payload)


def edit_admin_decision_message(
    admin_chat_id: str,
    message_id: int,
    html_text: str,
) -> None:
    telegram_api(
        "editMessageText",
        {
            "chat_id": admin_chat_id,
            "message_id": message_id,
            "text": html_text,
            "parse_mode": "HTML",
        },
    )


def ensure_long_polling_mode() -> None:
    """如曾設定 webhook，先移除，否則 getUpdates 不會運作。"""
    telegram_api("deleteWebhook", {"drop_pending_updates": "false"})


# ================== 5. 訂閱審批邏輯 ==================
def is_admin(user_id) -> bool:
    return bool(ADMIN_CHAT_ID) and normalize_chat_id(user_id) == ADMIN_CHAT_ID


def build_applicant_record(message: dict) -> dict:
    user = message.get("from", {}) or {}
    chat = message.get("chat", {}) or {}
    return {
        "name": display_name_from_user(user),
        "username": str(user.get("username", "") or ""),
        "user_id": normalize_chat_id(user.get("id", "")),
        "chat_type": str(chat.get("type", "")),
        "requested_at": utc_timestamp(),
    }


def notify_admin_of_request(chat_id: str, applicant: dict) -> bool:
    if not ADMIN_CHAT_ID:
        print("❌ 尚未設定 ADMIN_CHAT_ID，無法通知管理員。")
        return False

    username = applicant.get("username", "")
    username_text = f"@{html_escape_text(username)}" if username else "未設定"
    text = (
        "🔔 <b>新的新聞訂閱申請</b>\n\n"
        f"姓名：<b>{html_escape_text(applicant.get('name', ''))}</b>\n"
        f"Username：{username_text}\n"
        f"Chat ID：<code>{html_escape_text(chat_id)}</code>\n\n"
        "請選擇是否批准："
    )
    keyboard = {
        "inline_keyboard": [[
            {
                "text": "✅ 批准",
                "callback_data": f"approve:{chat_id}",
            },
            {
                "text": "❌ 拒絕",
                "callback_data": f"reject:{chat_id}",
            },
        ]]
    }
    return send_message_to(ADMIN_CHAT_ID, text, reply_markup=keyboard)


def handle_subscribe_request(message: dict) -> None:
    chat = message.get("chat", {}) or {}
    chat_id = normalize_chat_id(chat.get("id", ""))
    chat_type = str(chat.get("type", ""))

    if chat_type != "private":
        send_message_to(
            chat_id,
            "⚠️ 為保障私隱，請在與本 Bot 的私人對話中輸入 /subscribe。",
        )
        return

    if chat_id in BOT_STATE["subscribers"]:
        send_message_to(chat_id, "✅ 你的新聞訂閱已獲批准，無需再次申請。")
        return

    if chat_id in BOT_STATE["pending"]:
        send_message_to(chat_id, "⏳ 你的申請仍在等待管理員審批。")
        return

    applicant = build_applicant_record(message)
    BOT_STATE["pending"][chat_id] = applicant
    save_state()

    if notify_admin_of_request(chat_id, applicant):
        send_message_to(
            chat_id,
            "📨 已把你的訂閱申請送交管理員。批准後，Bot 會再通知你。",
        )
    else:
        # 若無法通知管理員，取消 pending，讓用戶稍後可以重新申請。
        BOT_STATE["pending"].pop(chat_id, None)
        save_state()
        send_message_to(
            chat_id,
            "❌ 暫時未能送出申請，請稍後再試或聯絡管理員。",
        )


def approve_subscriber(chat_id: str) -> tuple[bool, str]:
    chat_id = normalize_chat_id(chat_id)
    applicant = BOT_STATE["pending"].pop(chat_id, None)

    if chat_id in BOT_STATE["subscribers"]:
        save_state()
        return False, "這位用戶早已獲批准。"

    if not applicant:
        return False, "找不到待審批申請，可能已處理。"

    BOT_STATE["subscribers"][chat_id] = {
        **applicant,
        "approved_at": utc_timestamp(),
        "approved_by": ADMIN_CHAT_ID,
    }
    save_state()

    # 複製管理員現有的已發送紀錄，避免新訂閱者一獲批准便收到大量舊新聞。
    admin_history = SENT_MAP.get(ADMIN_CHAT_ID, set())
    SENT_MAP[chat_id] = set(admin_history)
    save_sent_map(SENT_MAP)

    sent = send_message_to(
        chat_id,
        "✅ <b>你的新聞訂閱申請已獲批准。</b>\n\n從現在開始，你會收到新的新聞快訊。",
    )
    if not sent:
        print(f"⚠️ 已批准 {chat_id}，但未能向該用戶發送確認訊息。")

    return True, "已批准訂閱。"


def reject_subscriber(chat_id: str) -> tuple[bool, str]:
    chat_id = normalize_chat_id(chat_id)
    applicant = BOT_STATE["pending"].pop(chat_id, None)
    if not applicant:
        return False, "找不到待審批申請，可能已處理。"

    save_state()
    send_message_to(
        chat_id,
        "❌ 你的新聞訂閱申請未獲批准。如有疑問，請聯絡管理員。",
    )
    return True, "已拒絕申請。"


def unsubscribe_chat(chat_id: str) -> bool:
    chat_id = normalize_chat_id(chat_id)
    removed = BOT_STATE["subscribers"].pop(chat_id, None)
    BOT_STATE["pending"].pop(chat_id, None)
    if removed:
        save_state()
        SENT_MAP.pop(chat_id, None)
        save_sent_map(SENT_MAP)
        return True
    save_state()
    return False


def format_subscriber_list() -> str:
    subscribers = BOT_STATE["subscribers"]
    if not subscribers:
        return "目前沒有已批准訂閱者。"

    lines = [f"👥 <b>已批准訂閱者：{len(subscribers)}</b>"]
    for index, (chat_id, record) in enumerate(subscribers.items(), start=1):
        name = html_escape_text(record.get("name", "未提供名稱"))
        username = record.get("username", "")
        username_text = f" @{html_escape_text(username)}" if username else ""
        lines.append(f"{index}. {name}{username_text} — <code>{chat_id}</code>")
    return "\n".join(lines)


def format_pending_list() -> str:
    pending = BOT_STATE["pending"]
    if not pending:
        return "目前沒有待審批申請。"

    lines = [f"⏳ <b>待審批申請：{len(pending)}</b>"]
    for index, (chat_id, record) in enumerate(pending.items(), start=1):
        name = html_escape_text(record.get("name", "未提供名稱"))
        username = record.get("username", "")
        username_text = f" @{html_escape_text(username)}" if username else ""
        lines.append(f"{index}. {name}{username_text} — <code>{chat_id}</code>")
    return "\n".join(lines)


def parse_command(text: str) -> tuple[str, list[str]]:
    parts = text.strip().split()
    if not parts:
        return "", []
    command = parts[0].split("@", 1)[0].lower()
    return command, parts[1:]


def handle_message_update(message: dict) -> None:
    chat = message.get("chat", {}) or {}
    chat_id = normalize_chat_id(chat.get("id", ""))
    sender = message.get("from", {}) or {}
    sender_id = normalize_chat_id(sender.get("id", ""))
    text = str(message.get("text", "") or "").strip()

    if not chat_id or not text.startswith("/"):
        return

    command, args = parse_command(text)

    if command in ("/start", "/help"):
        send_message_to(
            chat_id,
            "📰 <b>新聞快訊 Bot</b>\n\n"
            "輸入 /subscribe 申請訂閱。\n"
            "管理員批准後，你才會收到新聞。\n"
            "輸入 /status 查看申請狀態。\n"
            "輸入 /unsubscribe 取消訂閱。",
        )
        return

    if command == "/subscribe":
        handle_subscribe_request(message)
        return

    if command == "/status":
        if chat_id in BOT_STATE["subscribers"]:
            response = "✅ 你的訂閱已獲批准。"
        elif chat_id in BOT_STATE["pending"]:
            response = "⏳ 你的申請正在等待管理員審批。"
        else:
            response = "ℹ️ 你尚未申請訂閱。輸入 /subscribe 可以提出申請。"
        send_message_to(chat_id, response)
        return

    if command == "/unsubscribe":
        if unsubscribe_chat(chat_id):
            send_message_to(chat_id, "✅ 你已取消新聞訂閱。")
            if ADMIN_CHAT_ID and chat_id != ADMIN_CHAT_ID:
                name = display_name_from_user(sender)
                send_message_to(
                    ADMIN_CHAT_ID,
                    "ℹ️ <b>用戶已自行取消訂閱</b>\n\n"
                    f"姓名：{html_escape_text(name)}\n"
                    f"Chat ID：<code>{html_escape_text(chat_id)}</code>",
                )
        else:
            send_message_to(chat_id, "ℹ️ 你目前並非已批准訂閱者。")
        return

    # 以下為管理員專用指令。
    if command in ("/subscribers", "/pending", "/approve", "/reject", "/remove"):
        if not is_admin(sender_id):
            send_message_to(chat_id, "⛔ 你沒有管理員權限。")
            return

        if command == "/subscribers":
            send_message_to(chat_id, format_subscriber_list())
            return

        if command == "/pending":
            send_message_to(chat_id, format_pending_list())
            return

        if not args:
            send_message_to(chat_id, f"用法：{command} &lt;chat_id&gt;")
            return

        target_chat_id = normalize_chat_id(args[0])
        if command == "/approve":
            _, result_text = approve_subscriber(target_chat_id)
        elif command == "/reject":
            _, result_text = reject_subscriber(target_chat_id)
        else:
            removed = unsubscribe_chat(target_chat_id)
            result_text = "已移除訂閱者。" if removed else "找不到該訂閱者。"
            if removed:
                send_message_to(
                    target_chat_id,
                    "ℹ️ 管理員已停止你的新聞訂閱。",
                )
        send_message_to(chat_id, html_escape_text(result_text))
        return


def handle_callback_update(callback_query: dict) -> None:
    callback_id = str(callback_query.get("id", ""))
    sender = callback_query.get("from", {}) or {}
    sender_id = normalize_chat_id(sender.get("id", ""))
    data = str(callback_query.get("data", "") or "")
    callback_message = callback_query.get("message", {}) or {}

    if not is_admin(sender_id):
        answer_callback_query(callback_id, "你沒有管理員權限。")
        return

    if ":" not in data:
        answer_callback_query(callback_id, "無效操作。")
        return

    action, target_chat_id = data.split(":", 1)
    target_chat_id = normalize_chat_id(target_chat_id)

    if action == "approve":
        success, result_text = approve_subscriber(target_chat_id)
        decision = "✅ 已批准" if success else "ℹ️ 未有變更"
    elif action == "reject":
        success, result_text = reject_subscriber(target_chat_id)
        decision = "❌ 已拒絕" if success else "ℹ️ 未有變更"
    else:
        answer_callback_query(callback_id, "不支援的操作。")
        return

    answer_callback_query(callback_id, result_text)

    admin_message_chat = normalize_chat_id(
        (callback_message.get("chat", {}) or {}).get("id", ADMIN_CHAT_ID)
    )
    message_id = callback_message.get("message_id")
    if message_id:
        edit_admin_decision_message(
            admin_message_chat,
            int(message_id),
            f"{decision}\n\nChat ID：<code>{html_escape_text(target_chat_id)}</code>\n"
            f"結果：{html_escape_text(result_text)}",
        )


def poll_telegram_updates() -> None:
    offset = int(BOT_STATE.get("last_update_id", 0) or 0) + 1
    result = telegram_api(
        "getUpdates",
        {
            "offset": offset,
            "timeout": 0,
            "allowed_updates": json.dumps(["message", "callback_query"]),
        },
        timeout=10,
    )

    if not isinstance(result, list):
        return

    for update in result:
        update_id = int(update.get("update_id", 0) or 0)
        try:
            if "message" in update:
                handle_message_update(update["message"])
            elif "callback_query" in update:
                handle_callback_update(update["callback_query"])
        except Exception as exc:
            print(f"❌ 處理 Telegram update {update_id} 失敗：{exc}")
        finally:
            if update_id > int(BOT_STATE.get("last_update_id", 0) or 0):
                BOT_STATE["last_update_id"] = update_id
                save_state()


# ================== 6. 抓取與補全 ==================
def fetch_feed_entries(source_label: str, rss_url: str) -> list[tuple[str, str, str]]:
    entries = []

    if source_label == "📰 HK01":
        try:
            response = HTTP.get(
                rss_url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=15,
            )
            if response.status_code == 200:
                for item in response.json().get("items", [])[:15]:
                    info = item.get("data", {})
                    title = clean_title_simple(info.get("title", ""))
                    link = info.get("publishUrl", "")
                    pub_time = str(
                        info.get("publishTime", info.get("lastModified", ""))
                    )

                    if link and not link.startswith("http"):
                        link = f"https://www.hk01.com{link}"
                    if title and link:
                        entries.append((title, clean_url(link), pub_time))
        except Exception as exc:
            print(f"HK01 API 抓取錯誤：{exc}")

    elif source_label == "🔵 商台即時":
        try:
            response = HTTP.get(
                rss_url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=15,
            )
            if response.status_code == 200:
                content_list = response.json().get("response", {}).get("content", [])
                for item in content_list[:15]:
                    title = clean_title_simple(item.get("title", ""))
                    item_id = item.get("item_id", "")
                    uri_code = item.get("article_column", {}).get("uri_code", "local")
                    pub_time = str(item.get("display_ts", ""))
                    link = (
                        f"https://www.881903.com/news/{uri_code}/{item_id}"
                        if item_id
                        else ""
                    )
                    if title and link:
                        entries.append((title, clean_url(link), pub_time))
        except Exception as exc:
            print(f"商台 API 抓取錯誤：{exc}")
    

            
    elif "newsapi1.now.com" in rss_url:
        try:
            # 必須使用手機版 Headers，否則會被 Now 伺服器擋下
            headers = {
                "User-Agent": "Mozilla/5.0 (Linux; Android 15; Pixel 9) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Mobile Safari/537.36",
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Accept-Language": "zh-HK,zh;q=0.9,en-US;q=0.8,en;q=0.7",
                "Origin": "https://news.now.com",
                "Referer": "https://news.now.com/",
            }
            response = HTTP.get(
                rss_url,
                headers=headers,
                timeout=15,
            )
            if response.status_code == 200:
                data = response.json()
                # 兼容不同結構，通常清單放在根目錄，或放在 "data" / "news" 下
                items_list = data if isinstance(data, list) else data.get("news", data.get("data", []))
                
                for item in items_list[:15]:
                    title = clean_title_simple(item.get("title", ""))
                    # 確保能抓到 newsId，有些 API 節點叫 newsId 有些叫 id
                    news_id = item.get("newsId", item.get("id", ""))
                    link = f"https://news.now.com/home/local/player?newsId={news_id}" if news_id else item.get("link", "")
                    pub_time = str(item.get("publishDate", item.get("publishTime", "")))
                    
                    link = clean_url(link)
                    if title and link.startswith("http"):
                        entries.append((title, link, pub_time))
        except Exception as exc:
            print(f"Now API 抓取錯誤：{exc}")

    else:
        try:
            response = HTTP.get(
                rss_url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=15,
                verify=False,
            )
            feed = feedparser.parse(response.content)
            for entry in feed.entries[:15]:
                title = clean_title_simple(getattr(entry, "title", ""))
                link = (
                    getattr(entry, "link", "")
                    or getattr(entry, "id", "")
                    or ""
                ).strip()
                pub_time = str(
                    getattr(entry, "published", getattr(entry, "updated", ""))
                )

                if link.startswith("/"):
                    if "7vsPHGi" in rss_url:
                        link = f"https://www.i-cable.com{link}"
                    elif "tBTzOcf" in rss_url:
                        link = f"https://www.hkej.com{link}"
                    elif "Lk7D530m" in rss_url:
                        link = f"https://news.now.com{link}"
                    elif "hkcd" in rss_url or "pl.html" in rss_url:
                        link = f"https://www.hkcd.com.hk{link}"
                    elif "6oljXv" in rss_url or "C499xnj" in rss_url:
                        link = f"https://www.wenweipo.com{link}"
                    elif "59Pndw" in rss_url or "xbfGvXW" in rss_url:
                        link = f"https://www.dotdotnews.com{link}"
                    elif "KZGhq" in rss_url or "8fzf6zR" in rss_url:
                        link = f"https://www.orangenews.hk{link}"

                link = clean_url(link)
                if title and link.startswith("http"):
                    entries.append((title, link, pub_time))
        except Exception as exc:
            print(f"{source_label} RSS 抓取錯誤：{exc}")

    return entries


# ================== 7. 新聞業務邏輯 ==================
def notify_feed_error(message: str) -> None:
    if ADMIN_CHAT_ID:
        send_message_to(ADMIN_CHAT_ID, message)


def process_priority_news() -> None:
    """每 1 分鐘處理即時性較高的新聞。"""
    sources = [
        ("🏛 新聞稿", "https://www.info.gov.hk/gia/rss/general_zh.xml"),
        ("📻 RTHK 電台", "https://rthk.hk/rthk/news/rss/c_expressnews_clocal.xml"),
    ]

    for label, url in sources:
        items = fetch_feed_entries(label, url)
        threshold = CUSTOM_THRESHOLDS.get(label, DEFAULT_ALERT_THRESHOLD)

        if not items:
            FEED_ERRORS[label] = FEED_ERRORS.get(label, 0) + 1
            if FEED_ERRORS[label] == threshold:
                notify_feed_error(
                    f"⚠️ <b>系統警告：RSS 故障</b>\n\n"
                    f"發現 <b>{label}</b> 已連續 {threshold} 次無法抓取資料，請抽空檢查！"
                )
            continue

        if FEED_ERRORS.get(label, 0) >= threshold:
            notify_feed_error(
                f"✅ <b>系統通知：RSS 恢復</b>\n\n"
                f"<b>{label}</b> 來源已恢復正常連線！"
            )
        FEED_ERRORS[label] = 0

        is_rthk = label == "📻 RTHK 電台"
        for chat_id in get_approved_chat_ids():
            ensure_chat_key(SENT_MAP, chat_id)
            unsent = [
                item
                for item in items
                if get_unique_id(item[0], item[1], item[2]) not in SENT_MAP[chat_id]
            ]
            if not unsent:
                continue

            if is_rthk or len(unsent) > 1:
                lines = [f"<b>{label}（新消息）</b>"]
                for title, link, pub_time in unsent:
                    lines.append(f'• <a href="{link}">{html_escape_text(title)}</a>')
                                    
                # 新增：如果是新聞稿，在多筆清單底部加上 GNMIS 捷徑
                if label == "🏛 新聞稿":
                    lines.append('\n👉 <a href="https://www.isdnews.gov.hk/subscriber/loginpage?lang=0">GNMIS按此</a>')
                
                full_message = "\n".join(lines)
                if send_message_to(
                    chat_id,
                    full_message,
                    disable_preview=is_rthk,
                ):
                    for title, link, pub_time in unsent:
                        SENT_MAP[chat_id].add(get_unique_id(title, link, pub_time))
            else:
                title, link, pub_time = unsent[0]
                message = (
                    f'• <a href="{link}"><b>[{label}] '
                    f"{html_escape_text(title)}</b></a>"
                )
                
                # 新增：如果是新聞稿，在單筆新聞底部加上 GNMIS 捷徑
                if label == "🏛 新聞稿":
                    message += '\n\n👉 <a href="https://www.isdnews.gov.hk/subscriber/loginpage?lang=0">GNMIS按此</a>'
                    
                if send_message_to(chat_id, message):
                    SENT_MAP[chat_id].add(get_unique_id(title, link, pub_time))

            save_sent_map(SENT_MAP)


def process_grouped_news() -> None:
    """每 6 分鐘分段發送綜合新聞。"""
    group_sources = [
        ("💡 On.cc", "https://politepaul.com/fd/cTsVfG4sKP6c.xml"),
        ("📰 HK01", "https://web-data.api.hk01.com/v2/feed/category/0"),
        ("🐯 星島", "https://www.stheadline.com/rss"),
        ("📝 明報", "https://politepaul.com/fd/irsr7msXsno4.xml"),
        ("🐯 nowTV", "https://politepaul.com/fd/Lk7D530mgplN.xml"),
        ("🐯 Now新聞 (官方)", "https://newsapi1.now.com/pccw-news-api/api/getNewsList?category=119&pageSize=200&pageNo=1"),
        ("🐯 TVB", "https://politepaul.com/fd/BTyYcpixBubP.xml"),
        ("📺 有線新聞", "https://politepaul.com/fd/7vsPHGi1tzC9.xml"),
        ("📜 信報", "https://politepaul.com/fd/tBTzOcfkQWzF.xml"),
        ("📜 商報評論", "https://politepaul.com/fd/GO5FgkDR2gmP.xml"),
        ("🍊 橙新聞即時", "https://politepaul.com/fd/KZGhqIiTnOCq.xml"),
        ("🍊 橙新聞專欄", "https://politepaul.com/fd/8fzf6zRfoy6H.xml"),
        ("📜 文匯即時", "https://politepaul.com/fd/C499xnjIBdRm.xml"),
        ("📜 TOPick", "https://politepaul.com/fd/kpA6JdYHyRW3.xml"),
        ("🔵 點新聞即時", "https://politepaul.com/fd/xbfGvXWovqfk.xml"),
        ("🔵 點新聞評論", "https://politepaul.com/fd/59PndwU1mb82.xml"),
        ("🔵 文匯評論", "https://politepaul.com/fd/6oljXv2E75Pp.xml"),
        (
            "🔵 商台即時",
            "https://www.881903.com/api/news/section/morelist?news_column_id=11&limit=20",
        ),
    ]

    fetched = {}
    for label, url in group_sources:
        items = fetch_feed_entries(label, url)
        threshold = CUSTOM_THRESHOLDS.get(label, DEFAULT_ALERT_THRESHOLD)

        if not items:
            FEED_ERRORS[label] = FEED_ERRORS.get(label, 0) + 1
            if FEED_ERRORS[label] == threshold:
                notify_feed_error(
                    f"⚠️ <b>系統警告：RSS 故障</b>\n\n"
                    f"發現 <b>{label}</b> 已連續 {threshold} 次無法抓取資料，請抽空檢查！"
                )
        else:
            if FEED_ERRORS.get(label, 0) >= threshold:
                notify_feed_error(
                    f"✅ <b>系統通知：RSS 恢復</b>\n\n"
                    f"<b>{label}</b> 來源已恢復正常運作！"
                )
            FEED_ERRORS[label] = 0
            fetched[label] = items

    for chat_id in get_approved_chat_ids():
        ensure_chat_key(SENT_MAP, chat_id)
        sections = []
        all_new_ids = []

        for label, items in fetched.items():
            unsent = [
                item
                for item in items
                if get_unique_id(item[0], item[1], item[2]) not in SENT_MAP[chat_id]
            ]
            if unsent:
                lines = [f"<b>{label}</b>"] + [
                    f'• <a href="{item[1]}">{html_escape_text(item[0])}</a>'
                    for item in unsent[:4]
                ]
                sections.append("\n".join(lines))
                all_new_ids.extend(
                    get_unique_id(item[0], item[1], item[2]) for item in unsent
                )

        if sections:
            chunk_size = 8
            all_chunks_sent = True
            for index in range(0, len(sections), chunk_size):
                chunk = sections[index:index + chunk_size]
                full_message = (
                    f"<b>📰 綜合媒體快訊（{index // chunk_size + 1}）</b>\n\n"
                    + "\n\n".join(chunk)
                )
                if not send_message_to(chat_id, full_message, disable_preview=True):
                    all_chunks_sent = False

            # 只有全部分段均成功發送才標記為已讀，避免漏新聞。
            if all_chunks_sent:
                for unique_id in all_new_ids:
                    SENT_MAP[chat_id].add(unique_id)
                save_sent_map(SENT_MAP)


# ================== 8. 主循環 ==================
def validate_configuration() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("尚未設定 BOT_TOKEN")
    if not ADMIN_CHAT_ID:
        raise RuntimeError(
            "尚未設定 ADMIN_ID（或兼容名稱 ADMIN_CHAT_ID），亦無法從 CHAT_IDS 取得管理員 ID。"
        )


def main_loop() -> None:
    validate_configuration()
    ensure_long_polling_mode()

    print("✅ News Bot 啟動成功，Telegram 審批及新聞監控中……")
    print(f"✅ 管理員 Chat ID：{ADMIN_CHAT_ID}（來源：ADMIN_ID／兼容設定）")
    print(f"✅ 已批准訂閱者數目：{len(get_approved_chat_ids())}")

    next_telegram_poll = 0.0
    next_priority_news = 0.0
    next_grouped_news = 0.0

    while True:
        now_monotonic = time.monotonic()
        try:
            if now_monotonic >= next_telegram_poll:
                poll_telegram_updates()
                next_telegram_poll = now_monotonic + TELEGRAM_POLL_INTERVAL

            hk_now = datetime.now(pytz.timezone("Asia/Hong_Kong"))
            is_active_time = hk_now.hour >= 8 or (
                hk_now.hour == 0 and hk_now.minute <= 15
            )

            if is_active_time and now_monotonic >= next_priority_news:
                process_priority_news()
                next_priority_news = now_monotonic + PRIORITY_NEWS_INTERVAL

            if is_active_time and now_monotonic >= next_grouped_news:
                process_grouped_news()
                next_grouped_news = now_monotonic + GROUPED_NEWS_INTERVAL

        except Exception as exc:
            print(f"❌ 主循環錯誤：{exc}")

        time.sleep(1)


if __name__ == "__main__":
    main_loop()
