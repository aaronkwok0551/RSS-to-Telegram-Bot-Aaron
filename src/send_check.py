# send_check.py — 純文字逐一發送檢查（不牽涉 Markdown/HTML）
import os, requests, sys

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
_raw_ids = os.environ.get("CHAT_IDS", "") or os.environ.get("CHAT_ID", "")

if not BOT_TOKEN:
    print("❌ BOT_TOKEN 未設定"); sys.exit(1)
if not _raw_ids.strip():
    print("❌ 沒有設定 CHAT_IDS/CHAT_ID"); sys.exit(1)

CHAT_IDS = [x.strip() for x in _raw_ids.split(",") if x.strip()]
print("🔎 檢查這些 ID：", ", ".join(CHAT_IDS))

def send_plain(chat_id, text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    try:
        r = requests.post(url, data=payload, timeout=15)
        try:
            j = r.json()
            ok = j.get("ok", False); desc = j.get("description", "")
        except Exception:
            ok = False; desc = r.text[:300]
        print(f"[CHECK] to {chat_id}: status={r.status_code}, ok={ok}, desc={desc}")
    except Exception as e:
        print(f"[CHECK] error to {chat_id}: {e}")

for cid in CHAT_IDS:
    send_plain(cid, "純文字測試：你好，我是機器人 ✅")
print("✅ 測試完成，程式結束")
