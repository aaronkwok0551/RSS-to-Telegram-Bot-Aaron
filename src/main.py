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
BOT_TOKEN = os.environ.get("BOT_TOKEN")

_raw_ids = os.environ.get("CHAT_IDS", "").strip()
CHAT_IDS = []
if _raw_ids:
    CHAT_IDS = [cid.strip() for cid in _raw_ids.split(",") if cid.strip()]
else:
    if os.environ.get("CHAT_ID"):
        CHAT_IDS.append(os.environ.get("CHAT_ID").strip())
CHAT_IDS = [cid for cid in dict.fromkeys(CHAT_IDS) if cid]

SENT_FILE = "sent_urls_per_chat.json"
MAX_SENT_CACHE = 500 

# ================== 2. 工具函數 (最強防禦) ==================
def html_escape_text(s: str) -> str:
    """使用 Python 官方標準庫進行轉義，這是最安全的做法"""
    if not s: return ""
    # 先還原原本可能存在的轉義，再統一重新轉義
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
    """網址編碼：確保 URL 在 href 屬性中 100% 安全"""
    if not url: return ""
    url = url.strip()
    if "hkej.com" in url:
        url = url.replace("m.hkej.com", "www.hkej.com").replace("++", "")
    # 對網址進行編碼
    safe_url = quote(url, safe=":/%?=&")
    # 針對 HTML 屬性再次轉義 & 符號
    return html.escape(safe_url, quote=True)

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
    if chat_id not in sent_map:
        sent_map[chat_id] = set()

# ================== 4. Telegram 發送引擎 (修正版) ==================
def send_message_to(chat_id, html_text, disable_preview=False):
    if not BOT_TOKEN: return False
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": html_text,
        "parse_mode": "HTML",
        "disable_web_page_preview": disable_preview
    }
    try:
        r = requests.post(url, data=payload, timeout=20)
        if r.status_code != 200:
            # 如果失敗，在 Log 輸出錯誤原因，以便除錯
            print(f"❌ Telegram 發送失敗! ChatID: {chat_id}, Code: {r.status_code}, Resp: {r.text}")
            # 不再自動切換到純文字，我們必須修好 HTML 格式
            return False
        return True
    except Exception as e:
        print(f"Send Error: {e}")
        return False

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
                    if link and not link.startswith("http"):
                        link = f"https://www.hk01.com{link}"
                    if title and link:
                        entries.append((title, clean_url(link)))
        except Exception: pass
    else:
        try:
            r = requests.get(rss_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15, verify=False)
            feed = feedparser.parse(r.content)
            for entry in feed.entries[:15]:
                title = clean_title_simple(getattr(entry, "title", ""))
                link = (getattr(entry, "link", "") or getattr(entry, "id", "") or "").strip()
                if link.startswith("/"):
                    if "4xPuKWS" in rss_url: link = f"https://www.881903.com{link}"
                    elif "7vsPHGi" in rss_url: link = f"https://www.i-cable.com{link}"
                    elif "tBTzOcf" in rss_url: link = f"https://www.hkej.com{link}"
                    elif "X5o1ke3" in rss_url: link = f"https://topick.hket.com{link}"
                    elif "Lk7D530m" in rss_url: link = f"https://news.now.com{link}"
                    elif "hkcd" in rss_url or "pl.html" in rss_url: link = f"https://www.hkcd.com.hk{link}"
                    elif "6oljXv" in rss_url or "C499xnj" in rss_url: link = f"https://www.wenweipo.com{link}"
                    elif "59Pndw" in rss_url or "xbfGvXW" in rss_url: link = f"https://www.dotdotnews.com{link}"
                    elif "KZGhq" in rss_url or "8fzf6zR" in rss_url: link = f"https://www.orangenews.hk{link}"
                link = clean_url(link)
                if title and link.startswith("http"):
                    entries.append((title, link))
        except Exception as e: pass
    return entries

# ================== 6. 業務邏輯 ==================

def process_priority_news():
    """【每 1 分鐘】RTHK 使用列表形式發送"""
    sources = [
        ("🏛 新聞稿", "https://www.info.gov.hk/gia/rss/general_zh.xml"),
        ("📻 RTHK 電台", "https://rthk.hk/rthk/news/rss/c_expressnews_clocal.xml"),
    ]
    for label, url in sources:
        items = fetch_feed_entries(label, url)
        if not items: continue
        is_rthk = (label == "📻 RTHK 電台")
        
        for chat_id in CHAT_IDS:
            ensure_chat_key(SENT_MAP, chat_id)
            unsent = [it for it in items if it[1] not in SENT_MAP[chat_id]]
            if not unsent: continue
            
            # RTHK 或者是同時有多條新聞時，整合發送
            if is_rthk or len(unsent) > 1:
                lines = [f"<b>{label} (新消息)</b>"]
                for rt, rl in unsent:
                    lines.append(f"• <a href=\"{rl}\">{html_escape_text(rt)}</a>")
                full_msg = "\n".join(lines)
                if send_message_to(chat_id, full_msg, disable_preview=is_rthk):
                    for _, rl in unsent: SENT_MAP[chat_id].add(rl)
            else:
                # 單條新聞稿
                rt, rl = unsent[0]
                msg = f"• <a href=\"{rl}\"><b>[{label}] {html_escape_text(rt)}</b></a>"
                if send_message_to(chat_id, msg, disable_preview=False):
                    SENT_MAP[chat_id].add(rl)
            save_sent_map(SENT_MAP)

def process_grouped_news():
    """【每 6 分鐘】分段發送，確保不超過長度且 HTML 正確"""
    group_sources = [
        ("💡 On.cc", "https://rsshub-production-9dfc.up.railway.app/oncc/zh-hant/news"),
        ("📰 HK01", "https://web-data.api.hk01.com/v2/feed/category/0"),
        ("🐯 星島", "https://www.stheadline.com/rss"),
        ("📝 明報", "https://news.mingpao.com/rss/ins/all.xml"),
        ("🐯 nowTV", "https://politepaul.com/fd/Lk7D530mgplN.xml"),
        ("📺 有線新聞", "https://politepaul.com/fd/7vsPHGi1tzC9.xml"),
        ("📜 信報", "https://politepaul.com/fd/tBTzOcfkQWzF.xml"),
        ("🟢 TOPick", "https://politepaul.com/fd/X5o1ke3uTiH3.xml"),
        ("📜 商報評論", "https://politepaul.com/fd/GO5FgkDR2gmP.xml"),
        ("🍊 橙新聞即時", "https://politepaul.com/fd/KZGhqIiTnOCq.xml"),
        ("🍊 橙新聞專欄", "https://politepaul.com/fd/8fzf6zRfoy6H.xml"),
        ("📜 文匯即時", "https://politepaul.com/fd/C499xnjIBdRm.xml"),
        ("🔵 點新聞即時", "https://politepaul.com/fd/xbfGvXWovqfk.xml"),
        ("🔵 點新聞評論", "https://politepaul.com/fd/59PndwU1mb82.xml"),
        ("🔵 文匯評論", "https://politepaul.com/fd/6oljXv2E75Pp.xml"),
        ("🔵 商台即時", "https://politepaul.com/fd/4xPuKWS07tJs.xml")
    ]
    
    fetched = {}
    for label, url in group_sources:
        fetched[label] = fetch_feed_entries(label, url)

    for chat_id in CHAT_IDS:
        ensure_chat_key(SENT_MAP, chat_id)
        sections = []
        all_new_links = []
        
        # 遍歷所有來源
        for label, items in fetched.items():
            unsent = [it for it in items if it[1] not in SENT_MAP[chat_id]]
            if unsent:
                lines = [f"<b>{label}</b>"] + \
                        [f"• <a href=\"{it[1]}\">{html_escape_text(it[0])}</a>" for it in unsent[:4]]
                sections.append("\n".join(lines))
                all_new_links.extend([it[1] for it in unsent])

        # 分段發送邏輯：每 8 個來源發送一次訊息，防止過長或單一錯誤毀掉全部
        if sections:
            chunk_size = 8
            for i in range(0, len(sections), chunk_size):
                chunk = sections[i:i + chunk_size]
                full_msg = f"<b>📰 綜合媒體快訊 ({i//chunk_size + 1})</b>\n\n" + "\n\n".join(chunk)
                send_message_to(chat_id, full_msg, disable_preview=True)
            
            # 更新已發送清單
            for link in all_new_links:
                SENT_MAP[chat_id].add(link)
            save_sent_map(SENT_MAP)

# ================== 7. 主循環 ==================
def main_loop():
    print(f"✅ News Bot 啟動成功，監控中...")
    loop_count = 0
    while True:
        try:
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
