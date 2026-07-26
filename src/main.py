# -*- coding: utf-8 -*-
import os
import time
import json
import requests
import feedparser
from datetime import datetime
import pytz
import re
from urllib.parse import quote
import html
import urllib3

# ================== 0. 系統優化 ==================
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ================== 1. 基本設定 ==================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

# 讀取 Railway 環境變數中的管理員 ID
ADMIN_ID = os.environ.get("ADMIN_ID", "")  
CHAT_IDS_FILE = "chat_ids.json"
LAST_UPDATE_ID = 0  

def load_chat_ids():
    """從檔案載入 Chat ID，支援動態擴充"""
    cids = []
    if os.path.exists(CHAT_IDS_FILE):
        try:
            with open(CHAT_IDS_FILE, "r", encoding="utf-8") as f:
                cids = json.load(f)
        except Exception: pass
    else:
        _raw_ids = os.environ.get("CHAT_IDS", "").strip()
        if _raw_ids:
            cids = [cid.strip() for cid in _raw_ids.split(",") if cid.strip()]
        elif os.environ.get("CHAT_ID"):
            cids.append(os.environ.get("CHAT_ID").strip())
        save_chat_ids(cids)
    return list(dict.fromkeys(cids))

def save_chat_ids(cids):
    """儲存 Chat ID 到檔案"""
    with open(CHAT_IDS_FILE, "w", encoding="utf-8") as f:
        json.dump(cids, f)

# 初始化 CHAT_IDS
CHAT_IDS = load_chat_ids()

SENT_FILE = "sent_urls_per_chat.json"
MAX_SENT_CACHE = 500 

# 🚨 健康監控追蹤器 (設定各媒體容忍度)
FEED_ERRORS = {}
DEFAULT_ALERT_THRESHOLD = 10  # 預設：連續失敗 10 次 (約 1 小時) 發送警報

CUSTOM_THRESHOLDS = {
    "📜 商報評論": 50,  # 容忍連續空白約 5 小時
    "🔵 點新聞評論": 30, # 容忍連續空白約 3 小時
    "🔵 文匯評論": 30,
    "📝 明報": 15        # 容忍連續空白約 1.5 小時
}

# ================== 2. 工具函數 ==================
def html_escape_text(s: str) -> str:
    if not s: return ""
    s = html.unescape(str(s))
    return html.escape(s, quote=True)

def clean_title_simple(s: str) -> str:
    if not s: return ""
    s = re.sub(r"<[^>]+>", "", str(s))
    s = html.unescape(s)
    s = re.sub(r'\s*\d+(分鐘|小時|天)前.*', '', s)
    s = s.replace("分享", "")
    s = re.sub(r'\d{4}-\d{2}-\d{2}', '', s)
    s = re.sub(r'\s*\d{1,2}:\d{2}$', '', s.strip())
    return s.strip()

def clean_url(url: str) -> str:
    if not url: return ""
    url = url.strip()
    if "hkej.com" in url:
        url = url.replace("m.hkej.com", "www.hkej.com").replace("++", "")
    safe_url = quote(url, safe=":/%?=&")
    return html.escape(safe_url, quote=True)

def get_unique_id(title: str, url: str, pub_time: str) -> str:
    """標題、URL 與發佈時間合併的防重複憑證"""
    return f"{title}|{url}|{pub_time}"

# ================== 3. 數據持久化 ==================
def load_sent_map():
    try:
        if os.path.exists(SENT_FILE):
            with open(SENT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return {str(k): set(v) for k, v in data.items()}
    except Exception: pass
    return {}

def save_sent_map(sent_map):
    try:
        serializable = {}
        for k, v in sent_map.items():
            v_list = list(v)
            if len(v_list) > MAX_SENT_CACHE:
                v_list = v_list[-MAX_SENT_CACHE:]
            serializable[k] = v_list
        with open(SENT_FILE, "w", encoding="utf-8") as f:
            json.dump(serializable, f, ensure_ascii=False)
    except Exception as e:
        print(f"Save Error: {e}")

SENT_MAP = load_sent_map()

def ensure_chat_key(sent_map, chat_id):
    if str(chat_id) not in sent_map:
        sent_map[str(chat_id)] = set()

# ================== 4. Telegram 發送與接收引擎 ==================
def send_message_to(chat_id, html_text, disable_preview=False):
    if not BOT_TOKEN: return False
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": str(chat_id),
        "text": html_text,
        "parse_mode": "HTML",
        "disable_web_page_preview": disable_preview
    }
    try:
        r = requests.post(url, data=payload, timeout=20)
        if r.status_code != 200:
            print(f"❌ Telegram 發送失敗! ChatID: {chat_id}, Code: {r.status_code}, Resp: {r.text}")
            return False
        return True
    except Exception as e:
        print(f"Send Error: {e}")
        return False

def check_admin_commands():
    """檢查並處理 Admin 的指令"""
    global LAST_UPDATE_ID, CHAT_IDS
    if not BOT_TOKEN or not ADMIN_ID: return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    params = {"timeout": 5}
    if LAST_UPDATE_ID:
        params["offset"] = LAST_UPDATE_ID + 1

    try:
        r = requests.get(url, params=params, timeout=10)
        if r.status_code == 200:
            updates = r.json().get("result", [])
            for update in updates:
                LAST_UPDATE_ID = update["update_id"]
                message = update.get("message", {})
                text = message.get("text", "").strip()
                sender_id = str(message.get("from", {}).get("id", ""))
                chat_id = str(message.get("chat", {}).get("id", ""))

                if sender_id == ADMIN_ID:
                    if text.startswith("/add "):
                        new_id = text.split(" ")[1].strip()
                        if new_id not in CHAT_IDS:
                            CHAT_IDS.append(new_id)
                            save_chat_ids(CHAT_IDS)
                            send_message_to(chat_id, f"✅ 成功將 {new_id} 加入廣播清單！目前總訂閱數: {len(CHAT_IDS)}")
                        else:
                            send_message_to(chat_id, f"⚠️ {new_id} 已經在清單中了。")
                    
                    elif text == "/addme":
                        if chat_id not in CHAT_IDS:
                            CHAT_IDS.append(chat_id)
                            save_chat_ids(CHAT_IDS)
                            send_message_to(chat_id, "✅ 已將此對話框加入新聞廣播清單！")
                        else:
                            send_message_to(chat_id, "⚠️ 此對話框已經在接收清單中了。")
                    
                    elif text == "/list":
                        msg = "📊 <b>目前接收名單：</b>\n" + "\n".join(CHAT_IDS)
                        send_message_to(chat_id, msg)
                        
    except Exception as e:
        print(f"Fetch Update Error: {e}")

# ================== 5. 抓取與補全 ==================
def fetch_feed_entries(source_label, rss_url):
    entries = []
    if source_label == "📰 HK01":
        try:
            resp = requests.get(rss_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
            if resp.status_code == 200:
                for item in resp.json().get("items", [])[:15]:
                    info = item.get("data", {})
                    title = clean_title_simple(info.get("title", ""))
                    link = info.get("publishUrl", "")
                    pub_time = str(info.get("publishTime", info.get("lastModified", "")))
                    
                    if link and not link.startswith("http"):
                        link = f"https://www.hk01.com{link}"
                    if title and link:
                        entries.append((title, clean_url(link), pub_time))
        except Exception: pass
        
    elif source_label == "🔵 商台即時":
        try:
            resp = requests.get(rss_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
            if resp.status_code == 200:
                content_list = resp.json().get("response", {}).get("content", [])
                for item in content_list[:15]:
                    title = clean_title_simple(item.get("title", ""))
                    
                    item_id = item.get("item_id", "")
                    uri_code = item.get("article_column", {}).get("uri_code", "local")
                    pub_time = str(item.get("display_ts", ""))
                    
                    link = f"https://www.881903.com/news/{uri_code}/{item_id}" if item_id else ""
                    
                    if title and link:
                        entries.append((title, clean_url(link), pub_time))
        except Exception as e:
            print(f"商台 API 抓取錯誤: {e}")
            
    else:
        try:
            r = requests.get(rss_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15, verify=False)
            feed = feedparser.parse(r.content)
            for entry in feed.entries[:15]:
                title = clean_title_simple(getattr(entry, "title", ""))
                link = (getattr(entry, "link", "") or getattr(entry, "id", "") or "").strip()
                pub_time = str(getattr(entry, "published", getattr(entry, "updated", "")))
                
                # 🟢 專屬通道：如果是你的 Cloudflare Worker 來源，直接信任並確保有 http 開頭
                if "rssworkertopick" in rss_url:
                    if link.startswith("/"):
                        link = f"https://news.hket.com{link}"
                else:
                    # 原有的其他 RSS 補全邏輯
                    if link.startswith("/"):
                        if "7vsPHGi" in rss_url: link = f"https://www.i-cable.com{link}"
                        elif "tBTzOcf" in rss_url: link = f"https://www.hkej.com{link}"
                        elif "rssworkertopick" in rss_url: link = f"https://news.hket.com{link}"
                        elif "Lk7D530m" in rss_url: link = f"https://news.now.com{link}"
                        elif "hkcd" in rss_url or "pl.html" in rss_url: link = f"https://www.hkcd.com.hk{link}"
                        elif "6oljXv" in rss_url or "C499xnj" in rss_url: link = f"https://www.wenweipo.com{link}"
                        elif "59Pndw" in rss_url or "xbfGvXW" in rss_url: link = f"https://www.dotdotnews.com{link}"
                        elif "KZGhq" in rss_url or "8fzf6zR" in rss_url: link = f"https://www.orangenews.hk{link}"
                
                link = clean_url(link)
                if title and link.startswith("http"):
                    entries.append((title, link, pub_time))
        except Exception as e:
            print(f"Feed Parse Error ({source_label}): {e}")
            pass
    return entries

# ================== 6. 業務邏輯 ==================

def process_priority_news():
    """【每 1 分鐘】優先處理即時性高的新聞"""
    sources = [
        ("🏛 新聞稿", "https://www.info.gov.hk/gia/rss/general_zh.xml"),
        ("📻 RTHK 電台", "https://rthk.hk/rthk/news/rss/c_expressnews_clocal.xml"),
    ]
    for label, url in sources:
        items = fetch_feed_entries(label, url)
        threshold = CUSTOM_THRESHOLDS.get(label, DEFAULT_ALERT_THRESHOLD)
        
        if not items:
            FEED_ERRORS[label] = FEED_ERRORS.get(label, 0) + 1
            if FEED_ERRORS[label] == threshold and CHAT_IDS:
                alert_msg = f"⚠️ <b>系統警告：RSS 故障</b>\n\n發現 <b>{label}</b> 已連續 {threshold} 次無法抓取資料，請抽空檢查！"
                send_message_to(CHAT_IDS[0], alert_msg)
            continue
        else:
            if FEED_ERRORS.get(label, 0) >= threshold and CHAT_IDS:
                recover_msg = f"✅ <b>系統通知：RSS 恢復</b>\n\n<b>{label}</b> 來源已恢復正常連線！"
                send_message_to(CHAT_IDS[0], recover_msg)
            FEED_ERRORS[label] = 0
            
        is_rthk = (label == "📻 RTHK 電台")
        for chat_id in CHAT_IDS:
            ensure_chat_key(SENT_MAP, chat_id)
            unsent = [it for it in items if get_unique_id(it[0], it[1], it[2]) not in SENT_MAP[chat_id]]
            if not unsent: continue
            
            if is_rthk or len(unsent) > 1:
                lines = [f"<b>{label} (新消息)</b>"]
                for rt, rl, rp in unsent: 
                    lines.append(f"• <a href=\"{rl}\">{html_escape_text(rt)}</a>")
                full_msg = "\n".join(lines)
                if send_message_to(chat_id, full_msg, disable_preview=is_rthk):
                    for rt, rl, rp in unsent: 
                        SENT_MAP[chat_id].add(get_unique_id(rt, rl, rp))
            else:
                rt, rl, rp = unsent[0]
                msg = f"• <a href=\"{rl}\"><b>[{label}] {html_escape_text(rt)}</b></a>"
                if send_message_to(chat_id, msg, disable_preview=False):
                    SENT_MAP[chat_id].add(get_unique_id(rt, rl, rp))
            save_sent_map(SENT_MAP)

def process_grouped_news():
    """【每 6 分鐘】分段發送，確保不超過長度且 HTML 正確"""
    group_sources = [
        ("💡 On.cc", "https://politepaul.com/fd/cTsVfG4sKP6c.xml"),
        ("📰 HK01", "https://web-data.api.hk01.com/v2/feed/category/0"),
        ("🐯 星島", "https://www.stheadline.com/rss"),
        ("📝 明報", "https://politepaul.com/fd/irsr7msXsno4.xml"),
        ("🐯 nowTV", "https://politepaul.com/fd/Lk7D530mgplN.xml"),
        ("🐯 TVB", "https://politepaul.com/fd/BTyYcpixBubP.xml"),
        ("📺 有線新聞", "https://politepaul.com/fd/7vsPHGi1tzC9.xml"),
        ("📜 信報", "https://politepaul.com/fd/tBTzOcfkQWzF.xml"),
        ("🟢 TOPick", "https://rssworkertopick.aaronkwok0551.workers.dev/"),
        ("📜 商報評論", "https://politepaul.com/fd/GO5FgkDR2gmP.xml"),
        ("🍊 橙新聞即時", "https://politepaul.com/fd/KZGhqIiTnOCq.xml"),
        ("🍊 橙新聞專欄", "https://politepaul.com/fd/8fzf6zRfoy6H.xml"),
        ("📜 文匯即時", "https://politepaul.com/fd/C499xnjIBdRm.xml"),
        ("🔵 點新聞即時", "https://politepaul.com/fd/xbfGvXWovqfk.xml"),
        ("🔵 點新聞評論", "https://politepaul.com/fd/59PndwU1mb82.xml"),
        ("🔵 文匯評論", "https://politepaul.com/fd/6oljXv2E75Pp.xml"),
        ("🔵 商台即時", "https://www.881903.com/api/news/section/morelist?news_column_id=11&limit=20")
    ]
    
    fetched = {}
    for label, url in group_sources:
        items = fetch_feed_entries(label, url)
        threshold = CUSTOM_THRESHOLDS.get(label, DEFAULT_ALERT_THRESHOLD)
        
        if not items:
            FEED_ERRORS[label] = FEED_ERRORS.get(label, 0) + 1
            if FEED_ERRORS[label] == threshold and CHAT_IDS:
                alert_msg = f"⚠️ <b>系統警告：RSS 故障</b>\n\n發現 <b>{label}</b> 已連續 {threshold} 次無法抓取資料，有時間請通知Aaron去睇睇！"
                send_message_to(CHAT_IDS[0], alert_msg)
        else:
            if FEED_ERRORS.get(label, 0) >= threshold and CHAT_IDS:
                recover_msg = f"✅ <b>系統通知：RSS 恢復</b>\n\n<b>{label}</b> 來源已恢復正常運作！"
                send_message_to(CHAT_IDS[0], recover_msg)
            FEED_ERRORS[label] = 0
            
            fetched[label] = items

    for chat_id in CHAT_IDS:
        ensure_chat_key(SENT_MAP, chat_id)
        sections = []
        all_new_ids = []
        
        for label, items in fetched.items():
            unsent = [it for it in items if get_unique_id(it[0], it[1], it[2]) not in SENT_MAP[chat_id]]
            if unsent:
                lines = [f"<b>{label}</b>"] + \
                        [f"• <a href=\"{it[1]}\">{html_escape_text(it[0])}</a>" for it in unsent[:4]]
                sections.append("\n".join(lines))
                all_new_ids.extend([get_unique_id(it[0], it[1], it[2]) for it in unsent])

        if sections:
            chunk_size = 8
            for i in range(0, len(sections), chunk_size):
                chunk = sections[i:i + chunk_size]
                full_msg = f"<b>📰 綜合媒體快訊 ({i//chunk_size + 1})</b>\n\n" + "\n\n".join(chunk)
                send_message_to(chat_id, full_msg, disable_preview=True)
            
            for uid in all_new_ids:
                SENT_MAP[chat_id].add(uid)
            save_sent_map(SENT_MAP)

# ================== 7. 主循環 ==================
def main_loop():
    print(f"✅ News Bot 啟動成功，監控中...")
    loop_count = 0
    while True:
        try:
            check_admin_commands()

            hk_now = datetime.now(pytz.timezone("Asia/Hong_Kong"))
            is_active_time = ((hk_now.hour >= 8) or (hk_now.hour == 0 and hk_now.minute <= 15))
            if is_active_time:
                process_priority_news()
                if loop_count % 6 == 0:
                    process_grouped_news()
            loop_count += 1
        except Exception as e:
            print(f"❌ 循環錯誤: {e}")
        time.sleep(60)

if __name__ == "__main__":
    main_loop()
