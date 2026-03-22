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
# 忽略 SSL 警告 (部分舊媒體 RSS 證書過期)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ================== 1. 基本設定 ==================
# 從環境變數讀取 Token 和 Chat IDs
BOT_TOKEN = os.environ.get("BOT_TOKEN")

_raw_ids = os.environ.get("CHAT_IDS", "").strip()
CHAT_IDS = []
if _raw_ids:
    CHAT_IDS = [cid.strip() for cid in _raw_ids.split(",") if cid.strip()]
else:
    # 兼容舊版單一 CHAT_ID 設定
    if os.environ.get("CHAT_ID"):
        CHAT_IDS.append(os.environ.get("CHAT_ID").strip())
# 去重且排除空值
CHAT_IDS = [cid for cid in dict.fromkeys(CHAT_IDS) if cid]

# 數據快取設定
SENT_FILE = "sent_urls_per_chat.json"
MAX_SENT_CACHE = 500  # 每個群組最多保存 500 條紀錄

# ================== 2. 工具函數 ==================
def html_escape_text(s: str) -> str:
    """防止 Telegram 因為特殊字元解析失敗 (parse_mode='HTML')"""
    if not s: return ""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def clean_title_simple(s: str) -> str:
    """強力清洗標題：移除 HTML 標籤、時間後綴、以及商報特定 clutter"""
    if not s: return ""
    # 移除 HTML 標籤
    s = re.sub(r"<[^>]+>", "", str(s))
    s = html.unescape(s)
    
    # 【Now新聞 & RTHK 通用】移除「XX分鐘前」字眼
    s = re.sub(r'\s*\d+(分鐘|小時|天)前.*', '', s)
    
    # 【香港商報專用】移除末尾日期 (YYYY-MM-DD) 和 "分享" 字眼
    s = s.replace("分享", "")
    # 移除日期 YYYY-MM-DD
    s = re.sub(r'\d{4}-\d{2}-\d{2}', '', s)
    # 移除時間格式 HH:MM
    s = re.sub(r'\s*\d{1,2}:\d{2}$', '', s.strip())
    
    return s.strip()

def clean_url(url: str) -> str:
    """網址去重優化：修正 NowTV 參數、信報域名"""
    if not url: return ""
    url = url.strip()
    
    # 修正信報域名：由 m.hkej.com 轉為 www.hkej.com
    if "hkej.com" in url:
        url = url.replace("m.hkej.com", "www.hkej.com").replace("++", "")

    # NowTV 參數必須保留
    if "news.now.com" in url:
        return quote(url, safe=":/%?=&")
    
    # 其他媒體去掉統計參數確保去重精準
    url = url.split('?')[0] 
    return quote(url, safe=":/%?=&")

def strip_formatting_to_plain(s: str) -> str:
    """備用清洗：將 HTML 文字還原成純文本"""
    if not s: return ""
    return re.sub(r"<[^>]+>", "", s)

# ================== 3. 數據持久化 (防止重發) ==================
def load_sent_map():
    """啟動時讀取檔案"""
    try:
        if os.path.exists(SENT_FILE):
            with open(SENT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                # 將 JSON 的 list 轉回 set 方便快速比較
                return {str(k): set(v) for k, v in data.items()}
    except Exception: pass
    return {}

def save_sent_map(sent_map):
    """保存時限制緩存數量"""
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

# 初始化數據
SENT_MAP = load_sent_map()

def ensure_chat_key(sent_map, chat_id):
    if chat_id not in sent_map:
        sent_map[chat_id] = set()

# ================== 4. Telegram 發送引擎 ==================
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
        r = requests.post(url, data=payload, timeout=15)
        # 如果 HTML 解析失敗 (code 400)，嘗試發送純文本
        if r.status_code == 400: 
            payload["text"] = strip_formatting_to_plain(html_text)
            payload.pop("parse_mode", None)
            r = requests.post(url, data=payload, timeout=15)
        return r.json().get("ok", False)
    except Exception as e:
        print(f"Send Error: {e}")
        return False

# ================== 5. 抓取與域名補全邏輯 ==================
def fetch_feed_entries(source_label, rss_url):
    entries = []
    
    # --- 分支 A: HK01 (API) ---
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

    # --- 分支 B: 標準 RSS (包含 NowTV 與 PolitePol 域名補全) ---
    else:
        try:
            r = requests.get(rss_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15, verify=False)
            feed = feedparser.parse(r.content)
            for entry in feed.entries[:15]:
                # 抓取標題並清洗 HTML
                title = clean_title_simple(getattr(entry, "title", ""))
                link = (getattr(entry, "link", "") or getattr(entry, "id", "") or "").strip()
                
                # 自動補全域名邏輯 (針對 PolitePol 對相對連結處理的不足)
                if link.startswith("/"):
                    if "4xPuKWS" in rss_url: link = f"https://www.881903.com{link}"
                    elif "7vsPHGi" in rss_url: link = f"https://www.i-cable.com{link}"
                    elif "tBTzOcf" in rss_url: link = f"https://www.hkej.com{link}"
                    elif "X5o1ke3" in rss_url: link = f"https://topick.hket.com{link}"
                    elif "Lk7D530m" in rss_url: link = f"https://news.now.com{link}" # Now 新聞補全
                    elif "hkcd" in rss_url or "Pl.html" in rss_url: link = f"https://www.hkcd.com.hk{link}" # 商報補全
                    elif "6oljXv" in rss_url or "C499xnj" in rss_url: link = f"https://www.wenweipo.com{link}"
                    elif "59Pndw" in rss_url or "xbfGvXW" in rss_url: link = f"https://www.dotdotnews.com{link}"
                    elif "KZGhq" in rss_url or "8fzf6zR" in rss_url: link = f"https://www.orangenews.hk{link}"
                
                link = clean_url(link)
                if title and link.startswith("http"):
                    entries.append((title, link))
        except Exception as e:
            print(f"RSS Error ({source_label}): {e}")
            
    return entries

# ================== 6. 核心業務邏輯 ==================

def process_priority_news():
    """【每 1 分鐘執行】核心優先：政府新聞稿 & RTHK Local"""
    sources = [
        ("🏛 新聞稿", "https://www.info.gov.hk/gia/rss/general_zh.xml"),
        ("📻 RTHK 電台", "https://rthk.hk/rthk/news/rss/c_expressnews_clocal.xml"),
    ]
    for label, url in sources:
        items = fetch_feed_entries(label, url)
        if not items: continue
        for chat_id in CHAT_IDS:
            ensure_chat_key(SENT_MAP, chat_id)
            unsent = [it for it in items if it[1] not in SENT_MAP[chat_id]]
            if not unsent: continue
            
            # 優先新聞採用單條發送，最即時、醒目
            for rt, rl in unsent:
                msg = f"<b>[{label}] {html_escape_text(rt)}</b>\n<a href=\"{rl}\">🔗 查看詳情</a>"
                if send_message_to(chat_id, msg):
                    SENT_MAP[chat_id].add(rl)
            save_sent_map(SENT_MAP)

def process_grouped_news():
    """【每 6 分鐘執行一次】大眾媒體綜合整合報"""
    group_sources = [
        ("💡 On.cc", "https://rsshub-production-9dfc.up.railway.app/oncc/zh-hant/news"),
        ("📰 HK01", "https://web-data.api.hk01.com/v2/feed/category/0"),
        ("🐯 星島", "https://www.stheadline.com/rss"),
        ("📝 明報", "https://news.mingpao.com/rss/ins/all.xml"),
        ("🐯 nowTV", "https://politepaul.com/fd/Lk7D530mgplN.xml"), # <-- 已更新為 RSS
        ("📺 有線新聞", "https://politepaul.com/fd/7vsPHGi1tzC9.xml"),
        ("📜 信報", "https://politepaul.com/fd/tBTzOcfkQWzF.xml"),
        ("🟢 TOPick", "https://politepaul.com/fd/X5o1ke3uTiH3.xml"),
        ("📜 商報評論", "https://politepaul.com/fd/GO5FgkDR2gmP.xml"), 
        ("🍊 橙新聞", "https://politepaul.com/fd/KZGhqIiTnOCq.xml"),
        ("🍊 橙新聞", "https://politepaul.com/fd/8fzf6zRfoy6H.xml"),
        ("📜 文匯即時", "https://politepaul.com/fd/C499xnjIBdRm.xml"),
        ("🔵 點新聞即時", "https://politepaul.com/fd/xbfGvXWovqfk.xml"),
        ("🔵 點新聞評論", "https://politepaul.com/fd/59PndwU1mb82.xml"),
        ("🔵 文匯評論", "https://politepaul.com/fd/6oljXv2E75Pp.xml")
        ("🔵 商台即時", "https://politepaul.com/fd/4xPuKWS07tJs.xml")
    ]
    
    # 預先抓取所有來源
    fetched = {}
    for label, url in group_sources:
        fetched[label] = fetch_feed_entries(label, url)

    # 針對每個 Chat ID 進行整合發送
    for chat_id in CHAT_IDS:
        ensure_chat_key(SENT_MAP, chat_id)
        sections = []
        all_new_links = []

        for label, items in fetched.items():
            # 過濾未發送過的新聞
            unsent = [it for it in items if it[1] not in SENT_MAP[chat_id]]
            if unsent:
                # 每個來源最多顯示 4 條
                lines = [f"<b>{label}</b>"] + \
                        [f"• <a href=\"{it[1]}\">{html_escape_text(it[0])}</a>" for it in unsent[:4]]
                sections.append("\n".join(lines))
                # 紀錄所有待發送的網址用於更新緩存
                all_new_links.extend([it[1] for it in unsent])

        if sections:
            full_msg = "<b>📰 綜合媒體快訊 (6min)</b>\n\n" + "\n\n".join(sections)
            # 發送整合報 (disable_preview=True 防止訊息太碎)
            if send_message_to(chat_id, full_msg, disable_preview=True):
                for link in all_new_links:
                    SENT_MAP[chat_id].add(link)
                save_sent_map(SENT_MAP)

# ================== 7. 主循環 (時間與活躍期控制) ==================
def main_loop():
    print(f"✅ News Bot 啟動成功，監控中...")
    loop_count = 0
    while True:
        try:
            hk_now = datetime.now(pytz.timezone("Asia/Hong_Kong"))
            # 活躍時間設定 (早上8點到凌晨12:15)
            is_active_time = ((hk_now.hour >= 8) or (hk_now.hour == 0 and hk_now.minute <= 15))

            if is_active_time:
                # 1. 執行 1分鐘 優先檢查
                process_priority_news()
                
                # 2. 執行 6分鐘 整合檢查
                if loop_count % 6 == 0:
                    process_grouped_news()
            
            loop_count += 1
        except Exception as e:
            print(f"❌ 循環錯誤: {e}")
        
        # 暫停 60 秒 (組成 1 分鐘的圈)
        time.sleep(60)

if __name__ == "__main__":
    main_loop()
