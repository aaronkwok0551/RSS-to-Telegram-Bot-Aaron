# -*- coding: utf-8 -*-
"""
Telegram 新聞 Bot（管理員審批訂閱＋個人關鍵字置頂＋多線程加速＋15分鐘時間線版）

Railway 環境變數：
- BOT_TOKEN：Telegram Bot Token
- ADMIN_ID（或 ADMIN_CHAT_ID）：管理員的 Telegram numeric chat ID
- DATA_DIR：建議設為 /app/data，並把 Railway Volume 掛載到 /app/data
"""

import html
import json
import os
import re
import time
import urllib3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import quote

import feedparser
import pytz
import requests
from requests.adapters import HTTPAdapter


# ================== 0. 系統優化 ==================
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
HTTP = requests.Session()
_adapter = HTTPAdapter(pool_connections=25, pool_maxsize=25)
HTTP.mount("http://", _adapter)
HTTP.mount("https://", _adapter)


# ================== 1. 基本設定 ==================
BOT_TOKEN = (os.environ.get("BOT_TOKEN") or "").strip()

_raw_ids = os.environ.get("CHAT_IDS", "").strip()
INITIAL_CHAT_IDS = []
if _raw_ids:
    INITIAL_CHAT_IDS = [cid.strip() for cid in _raw_ids.split(",") if cid.strip()]
elif os.environ.get("CHAT_ID"):
    INITIAL_CHAT_IDS.append(os.environ.get("CHAT_ID", "").strip())
INITIAL_CHAT_IDS = list(dict.fromkeys(cid for cid in INITIAL_CHAT_IDS if cid))

ADMIN_ID = (
    os.environ.get("ADMIN_ID")
    or os.environ.get("ADMIN_CHAT_ID")
    or ""
).strip()

if not ADMIN_ID and INITIAL_CHAT_IDS:
    ADMIN_ID = INITIAL_CHAT_IDS[0]

ADMIN_CHAT_ID = ADMIN_ID

DATA_DIR = (os.environ.get("DATA_DIR") or ".").strip()
os.makedirs(DATA_DIR, exist_ok=True)

STATE_FILE = os.path.join(DATA_DIR, "subscriber_state.json")
SENT_FILE = os.path.join(DATA_DIR, "sent_urls_per_chat.json")
MAX_SENT_CACHE = 3000

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


def hk_time_str() -> str:
    return datetime.now(pytz.timezone("Asia/Hong_Kong")).strftime("%m/%d %H:%M")


def display_name_from_user(user: dict) -> str:
    first_name = str(user.get("first_name", "")).strip()
    last_name = str(user.get("last_name", "")).strip()
    full_name = " ".join(part for part in (first_name, last_name) if part).strip()
    return full_name or "未提供名稱"


def normalize_chat_id(value) -> str:
    return str(value).strip()


# ================== 3. 訂閱、關鍵字與發送紀錄持久化 ==================
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

    for chat_id in INITIAL_CHAT_IDS:
        state["subscribers"].setdefault(
            normalize_chat_id(chat_id),
            {
                "name": "Railway 初始訂閱者",
                "username": "",
                "approved_at": utc_timestamp(),
                "approved_by": "environment",
                "keywords": [],
            },
        )

    # 確保所有既有訂閱者都有 keywords 欄位
    for record in state["subscribers"].values():
        if isinstance(record, dict) and not isinstance(record.get("keywords"), list):
            record["keywords"] = []

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
    """使用 dict 作為有序集合，確保淘汰快取時依時間順序移除最舊紀錄。"""
    try:
        if os.path.exists(SENT_FILE):
            with open(SENT_FILE, "r", encoding="utf-8") as file:
                data = json.load(file)
            return {str(key): dict.fromkeys(value) for key, value in data.items()}
    except Exception as exc:
        print(f"⚠️ 讀取新聞發送紀錄失敗：{exc}")
    return {}


def save_sent_map(sent_map: dict) -> None:
    temp_file = f"{SENT_FILE}.tmp"
    try:
        serializable = {}
        for key, values in sent_map.items():
            value_list = list(values.keys())
            if len(value_list) > MAX_SENT_CACHE:
                value_list = value_list[-MAX_SENT_CACHE:]
                sent_map[key] = dict.fromkeys(value_list)
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
        sent_map[chat_id] = {}


def get_user_keywords(chat_id: str) -> list[str]:
    record = BOT_STATE["subscribers"].get(normalize_chat_id(chat_id), {})
    kws = record.get("keywords", [])
    return kws if isinstance(kws, list) else []


def match_keywords(title: str, keywords: list[str]) -> bool:
    if not title or not keywords:
        return False
    title_lower = title.lower()
    return any(kw.lower() in title_lower for kw in keywords if kw)


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


def register_bot_commands() -> None:
    """自動向 Telegram 註冊目前生效的指令選單（區分一般用戶與管理員）。"""
    user_commands = [
        {"command": "menu", "description": "顯示所有可用指令"},
        {"command": "add_kw", "description": "新增重點關注關鍵字（例：/add_kw 保安局 禁毒）"},
        {"command": "del_kw", "description": "刪除關鍵字（例：/del_kw 禁毒）"},
        {"command": "my_kw", "description": "查看我的關鍵字清單"},
        {"command": "subscribe", "description": "申請訂閱新聞快訊"},
        {"command": "status", "description": "查看訂閱狀態"},
        {"command": "unsubscribe", "description": "取消新聞訂閱"},
    ]
    telegram_api("setMyCommands", {"commands": json.dumps(user_commands, ensure_ascii=False)})

    if ADMIN_CHAT_ID:
        admin_commands = user_commands + [
            {"command": "subscribers", "description": "[管理員] 查看已批准訂閱者"},
            {"command": "pending", "description": "[管理員] 查看待審批申請"},
            {"command": "approve", "description": "[管理員] 批准訂閱 (/approve ID)"},
            {"command": "reject", "description": "[管理員] 拒絕申請 (/reject ID)"},
            {"command": "remove", "description": "[管理員] 移除訂閱者 (/remove ID)"},
            {"command": "clear_history", "description": "[管理員] 清空已發送新聞紀錄"},
        ]
        try:
            scope = {"type": "chat", "chat_id": int(ADMIN_CHAT_ID)}
            telegram_api(
                "setMyCommands",
                {
                    "commands": json.dumps(admin_commands, ensure_ascii=False),
                    "scope": json.dumps(scope),
                },
            )
        except Exception as exc:
            print(f"⚠️ 註冊管理員選單失敗：{exc}")


# ================== 5. 訂閱審批與指令邏輯 ==================
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
        "keywords": [],
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
        "keywords": applicant.get("keywords", []),
    }
    save_state()

    admin_history = SENT_MAP.get(ADMIN_CHAT_ID, {})
    SENT_MAP[chat_id] = dict(admin_history)
    save_sent_map(SENT_MAP)

    sent = send_message_to(
        chat_id,
        "✅ <b>你的新聞訂閱申請已獲批准。</b>\n\n"
        "從現在開始，你會收到新的新聞快訊。\n"
        "輸入 /menu 可查看所有指令（含個人關鍵字設定）。",
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


def format_menu_text(is_admin_user: bool) -> str:
    text = (
        "📰 <b>新聞快訊 Bot 指令選單</b>\n\n"
        "<b>【重點關鍵字（個人專屬）】</b>\n"
        "• /add_kw 關鍵字1 關鍵字2 — 新增置頂關注字眼\n"
        "• /del_kw 關鍵字 — 刪除關鍵字\n"
        "• /my_kw — 查看我的關鍵字清單\n\n"
        "<b>【訂閱管理】</b>\n"
        "• /subscribe — 申請訂閱新聞\n"
        "• /status — 查看申請／訂閱狀態\n"
        "• /unsubscribe — 取消訂閱\n"
        "• /menu — 顯示此指令選單"
    )
    if is_admin_user:
        text += (
            "\n\n<b>【管理員專用】</b>\n"
            "• /subscribers — 查看已批准訂閱者\n"
            "• /pending — 查看待審批申請\n"
            "• /approve &lt;chat_id&gt; — 手動批准訂閱\n"
            "• /reject &lt;chat_id&gt; — 手動拒絕申請\n"
            "• /remove &lt;chat_id&gt; — 移除訂閱者\n"
            "• /clear_history — 清空所有已發送新聞紀錄"
        )
    return text


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

    if command in ("/start", "/help", "/menu"):
        send_message_to(chat_id, format_menu_text(is_admin(sender_id)))
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

    # 個人關鍵字管理（限已批准訂閱者）
    if command in ("/add_kw", "/del_kw", "/my_kw"):
        if chat_id not in BOT_STATE["subscribers"]:
            send_message_to(chat_id, "⛔ 只有已批准訂閱者可使用關鍵字功能，請先輸入 /subscribe。")
            return

        record = BOT_STATE["subscribers"][chat_id]
        kws = record.setdefault("keywords", [])

        if command == "/my_kw":
            if not kws:
                send_message_to(
                    chat_id,
                    "ℹ️ 你目前尚未設定任何關鍵字。\n用法：<code>/add_kw 保安局 禁毒</code>",
                )
            else:
                kw_display = "、".join(html_escape_text(k) for k in kws)
                send_message_to(chat_id, f"🔍 <b>你的重點關注關鍵字（{len(kws)}）：</b>\n{kw_display}")
            return

        if not args:
            send_message_to(chat_id, f"用法：<code>{command} 關鍵字1 關鍵字2</code>")
            return

        if command == "/add_kw":
            added = []
            existing_lower = {k.lower() for k in kws}
            for kw in args:
                kw = kw.strip()
                if kw and kw.lower() not in existing_lower:
                    kws.append(kw)
                    existing_lower.add(kw.lower())
                    added.append(kw)
            save_state()
            if added:
                send_message_to(
                    chat_id,
                    f"✅ 已新增關鍵字：<b>{'、'.join(html_escape_text(k) for k in added)}</b>\n"
                    f"目前共 {len(kws)} 個關鍵字（輸入 /my_kw 查看）。",
                )
            else:
                send_message_to(chat_id, "ℹ️ 這些關鍵字已在你的清單中。")
            return

        if command == "/del_kw":
            targets_lower = {k.strip().lower() for k in args if k.strip()}
            new_kws = [k for k in kws if k.lower() not in targets_lower]
            removed_count = len(kws) - len(new_kws)
            record["keywords"] = new_kws
            save_state()
            if removed_count > 0:
                send_message_to(
                    chat_id,
                    f"🗑️ 已刪除 {removed_count} 個關鍵字，目前剩餘 {len(new_kws)} 個。",
                )
            else:
                send_message_to(chat_id, "ℹ️ 找不到指定的關鍵字。")
            return

    # 以下為管理員專用指令。
    if command in (
        "/subscribers",
        "/pending",
        "/approve",
        "/reject",
        "/remove",
        "/clear_history",
    ):
        if not is_admin(sender_id):
            send_message_to(chat_id, "⛔ 你沒有管理員權限。")
            return

        if command == "/subscribers":
            send_message_to(chat_id, format_subscriber_list())
            return

        if command == "/pending":
            send_message_to(chat_id, format_pending_list())
            return

        if command == "/clear_history":
            SENT_MAP.clear()
            save_sent_map(SENT_MAP)
            send_message_to(
                chat_id,
                "🗑️ 已清空所有已發送新聞紀錄，下一輪抓取將重新發送最新新聞。",
            )
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

    if "api.hk01.com" in rss_url:
        try:
            response = HTTP.get(
                rss_url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=30,
                Verify=False,
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
            print(f"{source_label} API 抓取錯誤：{exc}")

    elif source_label == "🔵 商台即時":
        try:
            response = HTTP.get(
                rss_url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=30,
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
                timeout=30,
            )
            if response.status_code == 200:
                data = response.json()
                items_list = (
                    data
                    if isinstance(data, list)
                    else data.get("news", data.get("data", []))
                )

                for item in items_list[:15]:
                    title = clean_title_simple(item.get("title", ""))
                    news_id = item.get("newsId", item.get("id", ""))
                    link = (
                        f"https://news.now.com/home/local/player?newsId={news_id}"
                        if news_id
                        else item.get("link", "")
                    )
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
                timeout=30,
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


def fetch_sources_concurrently(
    sources: list[tuple[str, str]], max_workers: int = 4
) -> list[tuple[str, list[tuple[str, str, str]]]]:
    """多線程同時抓取所有來源，並維持原本的媒體順序。"""
    results_map = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_label = {
            executor.submit(fetch_feed_entries, label, url): label
            for label, url in sources
        }
        for future, label in future_to_label.items():
            try:
                results_map[label] = future.result()
            except Exception as exc:
                print(f"❌ 多線程抓取 {label} 例外：{exc}")
                results_map[label] = []

    return [(label, results_map.get(label, [])) for label, _ in sources]


# ================== 7. 新聞業務邏輯 ==================
def notify_feed_error(message: str) -> None:
    if ADMIN_CHAT_ID:
        send_message_to(ADMIN_CHAT_ID, message)


def build_safe_chunks(sections: list[str], max_chars: int = 3500) -> list[str]:
    """按字元數自動分段，確保每則訊息不超過 Telegram 4096 字元上限。"""
    chunks = []
    current_chunk = []
    current_len = 0

    for sec in sections:
        if len(sec) > max_chars:
            if current_chunk:
                chunks.append("\n\n".join(current_chunk))
                current_chunk = []
                current_len = 0
            sub_lines = sec.split("\n")
            temp_lines = []
            temp_len = 0
            for line in sub_lines:
                if temp_lines and (temp_len + len(line) + 1 > max_chars):
                    chunks.append("\n".join(temp_lines))
                    temp_lines = [line]
                    temp_len = len(line)
                else:
                    temp_lines.append(line)
                    temp_len += len(line) + 1
            if temp_lines:
                chunks.append("\n".join(temp_lines))
            continue

        if current_chunk and (current_len + len(sec) + 2 > max_chars):
            chunks.append("\n\n".join(current_chunk))
            current_chunk = [sec]
            current_len = len(sec)
        else:
            current_chunk.append(sec)
            current_len += len(sec) + 2

    if current_chunk:
        chunks.append("\n\n".join(current_chunk))

    return chunks


def process_priority_news() -> None:
    """每 1 分鐘處理即時性較高的新聞。"""
    sources = [
        ("🏛 新聞稿", "https://www.info.gov.hk/gia/rss/general_zh.xml"),
        ("📻 RTHK 電台", "https://rthk.hk/rthk/news/rss/c_expressnews_clocal.xml"),
    ]

    fetched_list = fetch_sources_concurrently(sources, max_workers=2)

    for label, items in fetched_list:
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
        now_str = hk_time_str()
        for chat_id in get_approved_chat_ids():
            ensure_chat_key(SENT_MAP, chat_id)
            user_kws = get_user_keywords(chat_id)
            unsent = [
                item
                for item in items
                if get_unique_id(item[0], item[1], item[2]) not in SENT_MAP[chat_id]
            ]
            if not unsent:
                continue

            if is_rthk or len(unsent) > 1:
                lines = [f"<b>{label}（{now_str}）</b>"]
                for title, link, pub_time in unsent:
                    prefix = "🔴 " if match_keywords(title, user_kws) else "• "
                    lines.append(f'{prefix}<a href="{link}">{html_escape_text(title)}</a>')

                if label == "🏛 新聞稿":
                    lines.append('\n👉 <a href="https://www.isdnews.gov.hk/subscriber/loginpage?lang=0">GNMIS按此</a>')

              
                if send_message_to(
                    chat_id,
                    full_message,
                    disable_preview=is_rthk,
                ):
                    for title, link, pub_time in unsent:
                        SENT_MAP[chat_id][get_unique_id(title, link, pub_time)] = None
            else:
                title, link, pub_time = unsent[0]
                prefix = "🔴 " if match_keywords(title, user_kws) else "• "
                message = (
                    f'{prefix}<a href="{link}"><b>[{label}] '
                    f"{html_escape_text(title)}</b></a>"
                )

                if label == "🏛 新聞稿":
                    message += '\n\n👉 <a href="https://www.isdnews.gov.hk/subscriber/loginpage?lang=0">GNMIS按此</a>'

                message = with_time_divider(chat_id, message)
                if send_message_to(chat_id, message):
                    SENT_MAP[chat_id][get_unique_id(title, link, pub_time)] = None

            save_sent_map(SENT_MAP)


def process_grouped_news() -> None:
    """每 6 分鐘多線程抓取並分段發送綜合新聞。"""
    group_sources = [
        ("💡 On.cc", "https://politepaul.com/fd/cTsVfG4sKP6c.xml"),
        ("📰 HK01", "https://web-data.api.hk01.com/v2/feed/category/0"),
        ("📰 HK01突發", "https://web-data.api.hk01.com/v2/feed/category/6?limit=20&bucketId=00000"),
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

    fetched_list = fetch_sources_concurrently(group_sources, max_workers=4)
    fetched = {}

    for label, items in fetched_list:
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

    now_str = hk_time_str()

    for chat_id in get_approved_chat_ids():
        ensure_chat_key(SENT_MAP, chat_id)
        user_kws = get_user_keywords(chat_id)
        highlighted_lines = []
        normal_sections = []
        all_new_ids = []

        for label, items in fetched.items():
            unsent = [
                item
                for item in items
                if get_unique_id(item[0], item[1], item[2]) not in SENT_MAP[chat_id]
            ]
            if not unsent:
                continue

            normal_lines = []
            for title, link, pub_time in unsent:
                all_new_ids.append(get_unique_id(title, link, pub_time))
                if match_keywords(title, user_kws):
                    highlighted_lines.append(
                        f'🔴 <b>[{label}]</b> <a href="{link}">{html_escape_text(title)}</a>'
                    )
                else:
                    normal_lines.append(f'• <a href="{link}">{html_escape_text(title)}</a>')

            if normal_lines:
                normal_sections.append(f"<b>{label}</b>\n" + "\n".join(normal_lines))

        sections = []
        if highlighted_lines:# -*- coding: utf-8 -*-
"""
Telegram 新聞 Bot（管理員審批訂閱＋個人關鍵字置頂＋多線程加速版）

Railway 環境變數：
- BOT_TOKEN：Telegram Bot Token
- ADMIN_ID（或 ADMIN_CHAT_ID）：管理員的 Telegram numeric chat ID
- DATA_DIR：建議設為 /app/data，並把 Railway Volume 掛載到 /app/data
"""

import html
import json
import os
import re
import time
import urllib3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import quote

import feedparser
import pytz
import requests
from requests.adapters import HTTPAdapter


# ================== 0. 系統優化 ==================
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
HTTP = requests.Session()
_adapter = HTTPAdapter(pool_connections=25, pool_maxsize=25)
HTTP.mount("http://", _adapter)
HTTP.mount("https://", _adapter)


# ================== 1. 基本設定 ==================
BOT_TOKEN = (os.environ.get("BOT_TOKEN") or "").strip()

_raw_ids = os.environ.get("CHAT_IDS", "").strip()
INITIAL_CHAT_IDS = []
if _raw_ids:
    INITIAL_CHAT_IDS = [cid.strip() for cid in _raw_ids.split(",") if cid.strip()]
elif os.environ.get("CHAT_ID"):
    INITIAL_CHAT_IDS.append(os.environ.get("CHAT_ID", "").strip())
INITIAL_CHAT_IDS = list(dict.fromkeys(cid for cid in INITIAL_CHAT_IDS if cid))

ADMIN_ID = (
    os.environ.get("ADMIN_ID")
    or os.environ.get("ADMIN_CHAT_ID")
    or ""
).strip()

if not ADMIN_ID and INITIAL_CHAT_IDS:
    ADMIN_ID = INITIAL_CHAT_IDS[0]

ADMIN_CHAT_ID = ADMIN_ID

DATA_DIR = (os.environ.get("DATA_DIR") or ".").strip()
os.makedirs(DATA_DIR, exist_ok=True)

STATE_FILE = os.path.join(DATA_DIR, "subscriber_state.json")
SENT_FILE = os.path.join(DATA_DIR, "sent_urls_per_chat.json")
MAX_SENT_CACHE = 3000

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


def hk_time_str() -> str:
    return datetime.now(pytz.timezone("Asia/Hong_Kong")).strftime("%m/%d %H:%M")


def display_name_from_user(user: dict) -> str:
    first_name = str(user.get("first_name", "")).strip()
    last_name = str(user.get("last_name", "")).strip()
    full_name = " ".join(part for part in (first_name, last_name) if part).strip()
    return full_name or "未提供名稱"


def normalize_chat_id(value) -> str:
    return str(value).strip()


# ================== 3. 訂閱、關鍵字與發送紀錄持久化 ==================
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

    for chat_id in INITIAL_CHAT_IDS:
        state["subscribers"].setdefault(
            normalize_chat_id(chat_id),
            {
                "name": "Railway 初始訂閱者",
                "username": "",
                "approved_at": utc_timestamp(),
                "approved_by": "environment",
                "keywords": [],
            },
        )

    # 確保所有既有訂閱者都有 keywords 欄位
    for record in state["subscribers"].values():
        if isinstance(record, dict) and not isinstance(record.get("keywords"), list):
            record["keywords"] = []

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
    """使用 dict 作為有序集合，確保淘汰快取時依時間順序移除最舊紀錄。"""
    try:
        if os.path.exists(SENT_FILE):
            with open(SENT_FILE, "r", encoding="utf-8") as file:
                data = json.load(file)
            return {str(key): dict.fromkeys(value) for key, value in data.items()}
    except Exception as exc:
        print(f"⚠️ 讀取新聞發送紀錄失敗：{exc}")
    return {}


def save_sent_map(sent_map: dict) -> None:
    temp_file = f"{SENT_FILE}.tmp"
    try:
        serializable = {}
        for key, values in sent_map.items():
            value_list = list(values.keys())
            if len(value_list) > MAX_SENT_CACHE:
                value_list = value_list[-MAX_SENT_CACHE:]
                sent_map[key] = dict.fromkeys(value_list)
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
        sent_map[chat_id] = {}


def get_user_keywords(chat_id: str) -> list[str]:
    record = BOT_STATE["subscribers"].get(normalize_chat_id(chat_id), {})
    kws = record.get("keywords", [])
    return kws if isinstance(kws, list) else []


def match_keywords(title: str, keywords: list[str]) -> bool:
    if not title or not keywords:
        return False
    title_lower = title.lower()
    return any(kw.lower() in title_lower for kw in keywords if kw)


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


def register_bot_commands() -> None:
    """自動向 Telegram 註冊目前生效的指令選單（區分一般用戶與管理員）。"""
    user_commands = [
        {"command": "menu", "description": "顯示所有可用指令"},
        {"command": "add_kw", "description": "新增重點關注關鍵字（例：/add_kw 保安局 禁毒）"},
        {"command": "del_kw", "description": "刪除關鍵字（例：/del_kw 禁毒）"},
        {"command": "my_kw", "description": "查看我的關鍵字清單"},
        {"command": "subscribe", "description": "申請訂閱新聞快訊"},
        {"command": "status", "description": "查看訂閱狀態"},
        {"command": "unsubscribe", "description": "取消新聞訂閱"},
    ]
    telegram_api("setMyCommands", {"commands": json.dumps(user_commands, ensure_ascii=False)})

    if ADMIN_CHAT_ID:
        admin_commands = user_commands + [
            {"command": "subscribers", "description": "[管理員] 查看已批准訂閱者"},
            {"command": "pending", "description": "[管理員] 查看待審批申請"},
            {"command": "approve", "description": "[管理員] 批准訂閱 (/approve ID)"},
            {"command": "reject", "description": "[管理員] 拒絕申請 (/reject ID)"},
            {"command": "remove", "description": "[管理員] 移除訂閱者 (/remove ID)"},
            {"command": "clear_history", "description": "[管理員] 清空已發送新聞紀錄"},
        ]
        try:
            scope = {"type": "chat", "chat_id": int(ADMIN_CHAT_ID)}
            telegram_api(
                "setMyCommands",
                {
                    "commands": json.dumps(admin_commands, ensure_ascii=False),
                    "scope": json.dumps(scope),
                },
            )
        except Exception as exc:
            print(f"⚠️ 註冊管理員選單失敗：{exc}")


# ================== 5. 訂閱審批與指令邏輯 ==================
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
        "keywords": [],
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
        "keywords": applicant.get("keywords", []),
    }
    save_state()

    admin_history = SENT_MAP.get(ADMIN_CHAT_ID, {})
    SENT_MAP[chat_id] = dict(admin_history)
    save_sent_map(SENT_MAP)

    sent = send_message_to(
        chat_id,
        "✅ <b>你的新聞訂閱申請已獲批准。</b>\n\n"
        "從現在開始，你會收到新的新聞快訊。\n"
        "輸入 /menu 可查看所有指令（含個人關鍵字設定）。",
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


def format_menu_text(is_admin_user: bool) -> str:
    text = (
        "📰 <b>新聞快訊 Bot 指令選單</b>\n\n"
        "<b>【重點關鍵字（個人專屬）】</b>\n"
        "• /add_kw 關鍵字1 關鍵字2 — 新增置頂關注字眼\n"
        "• /del_kw 關鍵字 — 刪除關鍵字\n"
        "• /my_kw — 查看我的關鍵字清單\n\n"
        "<b>【訂閱管理】</b>\n"
        "• /subscribe — 申請訂閱新聞\n"
        "• /status — 查看申請／訂閱狀態\n"
        "• /unsubscribe — 取消訂閱\n"
        "• /menu — 顯示此指令選單"
    )
    if is_admin_user:
        text += (
            "\n\n<b>【管理員專用】</b>\n"
            "• /subscribers — 查看已批准訂閱者\n"
            "• /pending — 查看待審批申請\n"
            "• /approve &lt;chat_id&gt; — 手動批准訂閱\n"
            "• /reject &lt;chat_id&gt; — 手動拒絕申請\n"
            "• /remove &lt;chat_id&gt; — 移除訂閱者\n"
            "• /clear_history — 清空所有已發送新聞紀錄"
        )
    return text


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

    if command in ("/start", "/help", "/menu"):
        send_message_to(chat_id, format_menu_text(is_admin(sender_id)))
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

    # 個人關鍵字管理（限已批准訂閱者）
    if command in ("/add_kw", "/del_kw", "/my_kw"):
        if chat_id not in BOT_STATE["subscribers"]:
            send_message_to(chat_id, "⛔ 只有已批准訂閱者可使用關鍵字功能，請先輸入 /subscribe。")
            return

        record = BOT_STATE["subscribers"][chat_id]
        kws = record.setdefault("keywords", [])

        if command == "/my_kw":
            if not kws:
                send_message_to(
                    chat_id,
                    "ℹ️ 你目前尚未設定任何關鍵字。\n用法：<code>/add_kw 保安局 禁毒</code>",
                )
            else:
                kw_display = "、".join(html_escape_text(k) for k in kws)
                send_message_to(chat_id, f"🔍 <b>你的重點關注關鍵字（{len(kws)}）：</b>\n{kw_display}")
            return

        if not args:
            send_message_to(chat_id, f"用法：<code>{command} 關鍵字1 關鍵字2</code>")
            return

        if command == "/add_kw":
            added = []
            existing_lower = {k.lower() for k in kws}
            for kw in args:
                kw = kw.strip()
                if kw and kw.lower() not in existing_lower:
                    kws.append(kw)
                    existing_lower.add(kw.lower())
                    added.append(kw)
            save_state()
            if added:
                send_message_to(
                    chat_id,
                    f"✅ 已新增關鍵字：<b>{'、'.join(html_escape_text(k) for k in added)}</b>\n"
                    f"目前共 {len(kws)} 個關鍵字（輸入 /my_kw 查看）。",
                )
            else:
                send_message_to(chat_id, "ℹ️ 這些關鍵字已在你的清單中。")
            return

        if command == "/del_kw":
            targets_lower = {k.strip().lower() for k in args if k.strip()}
            new_kws = [k for k in kws if k.lower() not in targets_lower]
            removed_count = len(kws) - len(new_kws)
            record["keywords"] = new_kws
            save_state()
            if removed_count > 0:
                send_message_to(
                    chat_id,
                    f"🗑️ 已刪除 {removed_count} 個關鍵字，目前剩餘 {len(new_kws)} 個。",
                )
            else:
                send_message_to(chat_id, "ℹ️ 找不到指定的關鍵字。")
            return

    # 以下為管理員專用指令。
    if command in (
        "/subscribers",
        "/pending",
        "/approve",
        "/reject",
        "/remove",
        "/clear_history",
    ):
        if not is_admin(sender_id):
            send_message_to(chat_id, "⛔ 你沒有管理員權限。")
            return

        if command == "/subscribers":
            send_message_to(chat_id, format_subscriber_list())
            return

        if command == "/pending":
            send_message_to(chat_id, format_pending_list())
            return

        if command == "/clear_history":
            SENT_MAP.clear()
            save_sent_map(SENT_MAP)
            send_message_to(
                chat_id,
                "🗑️ 已清空所有已發送新聞紀錄，下一輪抓取將重新發送最新新聞。",
            )
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

    if "api.hk01.com" in rss_url:
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
            print(f"{source_label} API 抓取錯誤：{exc}")

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
                items_list = (
                    data
                    if isinstance(data, list)
                    else data.get("news", data.get("data", []))
                )

                for item in items_list[:15]:
                    title = clean_title_simple(item.get("title", ""))
                    news_id = item.get("newsId", item.get("id", ""))
                    link = (
                        f"https://news.now.com/home/local/player?newsId={news_id}"
                        if news_id
                        else item.get("link", "")
                    )
                    pub_time = str(item.get("publishDate", item.get("publishTime", "")))

                    link = clean_url(link)
                    if title and link.startswith("http"):
                        entries.append((title, link, pub_time))
        except Exception as exc:
            print(f"Now API 抓取錯誤：{exc}")

    else:
        for attempt in range(2):
            try:
                response = HTTP.get(
                    rss_url,
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=30,
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
                if entries:
                    break
            except Exception as exc:
                if attempt == 1:
                    print(f"{source_label} RSS 抓取錯誤：{exc}")
                else:
                    time.sleep(2)

    return entries


def fetch_sources_concurrently(
    sources: list[tuple[str, str]], max_workers: int = 4
) -> list[tuple[str, list[tuple[str, str, str]]]]:
    """多線程同時抓取所有來源，並維持原本的媒體順序。"""
    results_map = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_label = {
            executor.submit(fetch_feed_entries, label, url): label
            for label, url in sources
        }
        for future, label in future_to_label.items():
            try:
                results_map[label] = future.result()
            except Exception as exc:
                print(f"❌ 多線程抓取 {label} 例外：{exc}")
                results_map[label] = []

    return [(label, results_map.get(label, [])) for label, _ in sources]


# ================== 7. 新聞業務邏輯 ==================
def notify_feed_error(message: str) -> None:
    if ADMIN_CHAT_ID:
        send_message_to(ADMIN_CHAT_ID, message)


def build_safe_chunks(sections: list[str], max_chars: int = 3500) -> list[str]:
    """按字元數自動分段，確保每則訊息不超過 Telegram 4096 字元上限。"""
    chunks = []
    current_chunk = []
    current_len = 0

    for sec in sections:
        if len(sec) > max_chars:
            if current_chunk:
                chunks.append("\n\n".join(current_chunk))
                current_chunk = []
                current_len = 0
            sub_lines = sec.split("\n")
            temp_lines = []
            temp_len = 0
            for line in sub_lines:
                if temp_lines and (temp_len + len(line) + 1 > max_chars):
                    chunks.append("\n".join(temp_lines))
                    temp_lines = [line]
                    temp_len = len(line)
                else:
                    temp_lines.append(line)
                    temp_len += len(line) + 1
            if temp_lines:
                chunks.append("\n".join(temp_lines))
            continue

        if current_chunk and (current_len + len(sec) + 2 > max_chars):
            chunks.append("\n\n".join(current_chunk))
            current_chunk = [sec]
            current_len = len(sec)
        else:
            current_chunk.append(sec)
            current_len += len(sec) + 2

    if current_chunk:
        chunks.append("\n\n".join(current_chunk))

    return chunks


def process_priority_news() -> None:
    """每 1 分鐘處理即時性較高的新聞。"""
    sources = [
        ("🏛 新聞稿", "https://www.info.gov.hk/gia/rss/general_zh.xml"),
        ("📻 RTHK 電台", "https://rthk.hk/rthk/news/rss/c_expressnews_clocal.xml"),
    ]

    fetched_list = fetch_sources_concurrently(sources, max_workers=2)

    for label, items in fetched_list:
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
        now_str = hk_time_str()
        for chat_id in get_approved_chat_ids():
            ensure_chat_key(SENT_MAP, chat_id)
            user_kws = get_user_keywords(chat_id)
            unsent = [
                item
                for item in items
                if get_unique_id(item[0], item[1], item[2]) not in SENT_MAP[chat_id]
            ]
            if not unsent:
                continue

            if is_rthk or len(unsent) > 1:
                lines = [f"<b>{label}（{now_str}）</b>"]
                for title, link, pub_time in unsent:
                    prefix = "🔴 " if match_keywords(title, user_kws) else "• "
                    lines.append(f'{prefix}<a href="{link}">{html_escape_text(title)}</a>')

                if label == "🏛 新聞稿":
                    lines.append('\n👉 <a href="https://www.isdnews.gov.hk/subscriber/loginpage?lang=0">GNMIS按此</a>')

                full_message = "\n".join(lines)
                if send_message_to(
                    chat_id,
                    full_message,
                    disable_preview=is_rthk,
                ):
                    for title, link, pub_time in unsent:
                        SENT_MAP[chat_id][get_unique_id(title, link, pub_time)] = None
            else:
                title, link, pub_time = unsent[0]
                prefix = "🔴 " if match_keywords(title, user_kws) else "• "
                message = (
                    f'{prefix}<a href="{link}"><b>[{label}] '
                    f"{html_escape_text(title)}</b></a>"
                )

                if label == "🏛 新聞稿":
                    message += '\n\n👉 <a href="https://www.isdnews.gov.hk/subscriber/loginpage?lang=0">GNMIS按此</a>'

                if send_message_to(chat_id, message):
                    SENT_MAP[chat_id][get_unique_id(title, link, pub_time)] = None

            save_sent_map(SENT_MAP)


def process_grouped_news() -> None:
    """每 6 分鐘多線程抓取並分段發送綜合新聞。"""
    group_sources = [
        ("💡 On.cc", "https://politepaul.com/fd/cTsVfG4sKP6c.xml"),
        ("📰 HK01", "https://web-data.api.hk01.com/v2/feed/category/0"),
        ("📰 HK01突發", "https://web-data.api.hk01.com/v2/feed/category/6?limit=20&bucketId=00000"),
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

    fetched_list = fetch_sources_concurrently(group_sources, max_workers=4)
    fetched = {}

    for label, items in fetched_list:
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

    now_str = hk_time_str()

    for chat_id in get_approved_chat_ids():
        ensure_chat_key(SENT_MAP, chat_id)
        user_kws = get_user_keywords(chat_id)
        highlighted_lines = []
        normal_sections = []
        all_new_ids = []

        for label, items in fetched.items():
            unsent = [
                item
                for item in items
                if get_unique_id(item[0], item[1], item[2]) not in SENT_MAP[chat_id]
            ]
            if not unsent:
                continue

            normal_lines = []
            for title, link, pub_time in unsent:
                all_new_ids.append(get_unique_id(title, link, pub_time))
                if match_keywords(title, user_kws):
                    highlighted_lines.append(
                        f'🔴 <b>[{label}]</b> <a href="{link}">{html_escape_text(title)}</a>'
                    )
                else:
                    normal_lines.append(f'• <a href="{link}">{html_escape_text(title)}</a>')

            if normal_lines:
                normal_sections.append(f"<b>{label}</b>\n" + "\n".join(normal_lines))

        sections = []
        if highlighted_lines:
            highlight_block = (
                "<b>🚨 重點關注（命中關鍵字）</b>\n"
                + "\n".join(highlighted_lines)
                + "\n──────────────"
            )
            sections.append(highlight_block)
        sections.extend(normal_sections)

        if sections:
            chunks = build_safe_chunks(sections, max_chars=3500)
            total_chunks = len(chunks)
            all_chunks_sent = True

            for index, chunk_text in enumerate(chunks, start=1):
                header_suffix = f" · {index}/{total_chunks}" if total_chunks > 1 else ""
                full_message = (
                    f"<b>📰 綜合媒體快訊（{now_str}{header_suffix}）</b>\n\n"
                    + chunk_text
                )
                if not send_message_to(chat_id, full_message, disable_preview=True):
                    all_chunks_sent = False

            if all_chunks_sent:
                for unique_id in all_new_ids:
                    SENT_MAP[chat_id][unique_id] = None
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
    register_bot_commands()

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
            highlight_block = (
                "<b>🚨 重點關注（命中關鍵字）</b>\n"
                + "\n".join(highlighted_lines)
                + "\n──────────────"
            )
            sections.append(highlight_block)
        sections.extend(normal_sections)

        if sections:
            chunks = build_safe_chunks(sections, max_chars=3500)
            total_chunks = len(chunks)
            all_chunks_sent = True

            for index, chunk_text in enumerate(chunks, start=1):
                header_suffix = f" · {index}/{total_chunks}" if total_chunks > 1 else ""
                full_message = (
                    f"<b>📰 綜合媒體快訊（{now_str}{header_suffix}）</b>\n\n"
                    + chunk_text
                )
                if index == 1:
                    full_message = with_time_divider(chat_id, full_message)
                if not send_message_to(chat_id, full_message, disable_preview=True):
                    all_chunks_sent = False

            if all_chunks_sent:
                for unique_id in all_new_ids:
                    SENT_MAP[chat_id][unique_id] = None
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
    register_bot_commands()

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
