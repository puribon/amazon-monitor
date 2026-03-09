#!/usr/bin/env python3
"""
Amazon 在庫監視ツール - Railway デプロイ版
設定はすべて環境変数から読み込みます（.env または Railwayのダッシュボード）
"""

import asyncio
import os
import time
import json
import smtplib
import logging
import random
import re
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests
from bs4 import BeautifulSoup

# ============================================================
#  環境変数から設定を読み込む
# ============================================================

def get_products():
    """
    環境変数 PRODUCTS からJSON形式で商品リストを読み込む
    例: [{"asin":"B0CXXXXXXXXX","name":"PS5"},{"asin":"B0XXXXXXXXXX","name":"Switch"}]
    """
    raw = os.environ.get("PRODUCTS", "[]")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.error("PRODUCTS の JSON が正しくありません。例: [{\"asin\":\"B0XXXXXXXXXX\",\"name\":\"商品名\"}]")
        return []

CONFIG = {
    "email": {
        "smtp_host": os.environ.get("SMTP_HOST", "smtp.gmail.com"),
        "smtp_port": int(os.environ.get("SMTP_PORT", "587")),
        "username":  os.environ.get("EMAIL_USER", ""),
        "password":  os.environ.get("EMAIL_PASS", ""),
        "to":        os.environ.get("EMAIL_TO",   ""),
    },
    "interval_seconds":      float(os.environ.get("INTERVAL",        "10")),
    "jitter_seconds":        float(os.environ.get("JITTER",           "5")),
    "captcha_backoff_initial": int(os.environ.get("CAPTCHA_BACKOFF",  "60")),
    "captcha_backoff_max":     int(os.environ.get("CAPTCHA_MAX",     "600")),
    "request_timeout":         int(os.environ.get("REQUEST_TIMEOUT",  "12")),
}

# ============================================================
#  ロギング（Railway はコンソール出力をそのまま表示）
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-5s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("AmazonMonitor")

# ============================================================
#  User-Agent リスト
# ============================================================

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]

# ============================================================
#  セッション管理（CAPTCHA バックオフ付き）
# ============================================================

class SessionPool:
    def __init__(self):
        self._session = requests.Session()
        self._captcha_backoff = CONFIG["captcha_backoff_initial"]
        self._blocked_until = 0.0

    def report_captcha(self):
        wait = min(self._captcha_backoff, CONFIG["captcha_backoff_max"])
        self._blocked_until = time.time() + wait
        logger.warning(f"CAPTCHA検出。{wait}秒バックオフ中...")
        self._captcha_backoff = min(self._captcha_backoff * 2, CONFIG["captcha_backoff_max"])

    def reset_backoff(self):
        self._captcha_backoff = CONFIG["captcha_backoff_initial"]

    @property
    def is_blocked(self):
        return time.time() < self._blocked_until

    @property
    def block_remaining(self):
        return max(0.0, self._blocked_until - time.time())

    @property
    def session(self):
        return self._session

session_pool = SessionPool()

# ============================================================
#  在庫チェック
# ============================================================

IN_STOCK_SIGNALS    = ["カートに入れる", "今すぐ購入", "Add to Cart", "add-to-cart-button", "In Stock"]
OUT_OF_STOCK_SIGNALS = ["現在在庫切れです", "在庫切れ", "Currently unavailable", "currently-unavailable", "この商品は現在お取り扱いできません"]


def extract_asin(value: str):
    value = value.strip()
    if re.match(r"^[A-Z0-9]{10}$", value, re.IGNORECASE):
        return value.upper()
    for pattern in [r"/dp/([A-Z0-9]{10})", r"/gp/product/([A-Z0-9]{10})", r"asin=([A-Z0-9]{10})"]:
        m = re.search(pattern, value, re.IGNORECASE)
        if m:
            return m.group(1).upper()
    return None


def build_url(asin: str) -> str:
    return f"https://www.amazon.co.jp/dp/{asin}"


def check_stock_sync(url: str) -> str:
    if session_pool.is_blocked:
        return "blocked"

    headers = {
        "User-Agent":      random.choice(USER_AGENTS),
        "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8",
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control":   "no-cache",
        "DNT":             "1",
    }

    try:
        resp = session_pool.session.get(url, headers=headers, timeout=CONFIG["request_timeout"])
        resp.raise_for_status()
        html = resp.text

        if "captcha" in html.lower() or "Type the characters" in html:
            session_pool.report_captcha()
            return "blocked"

        session_pool.reset_backoff()
        soup = BeautifulSoup(html, "lxml")

        avail = soup.find(id="availability")
        if avail:
            text = avail.get_text(" ", strip=True)
            if any(s in text for s in ["在庫あり", "In Stock", "通常", "入荷予定"]):
                return "in_stock"
            if any(s in text for s in ["在庫切れ", "Currently unavailable", "現在在庫切れ"]):
                return "out_of_stock"

        if soup.find(id="add-to-cart-button"):
            return "in_stock"

        if any(s in html for s in OUT_OF_STOCK_SIGNALS):
            return "out_of_stock"
        if any(s in html for s in IN_STOCK_SIGNALS):
            return "in_stock"

        return "unknown"

    except requests.Timeout:
        return "unknown"
    except requests.RequestException as e:
        logger.debug(f"リクエストエラー: {e}")
        return "unknown"

# ============================================================
#  メール通知
# ============================================================

def send_email(product_name: str, asin: str, url: str):
    cfg = CONFIG["email"]
    if not cfg["username"] or not cfg["password"]:
        logger.warning("メール設定が未入力のため通知をスキップします (EMAIL_USER / EMAIL_PASS)")
        return

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    subject = f"🎉 在庫復活！ {product_name}"
    body_html = f"""
<html><body style="font-family:sans-serif;max-width:600px;margin:0 auto;padding:20px;background:#f4f4f4">
  <div style="background:white;border-radius:8px;padding:24px;box-shadow:0 2px 8px rgba(0,0,0,0.1)">
    <h2 style="color:#e47911;margin-top:0">🎉 Amazon 在庫復活！</h2>
    <table style="border-collapse:collapse;width:100%;margin-bottom:20px">
      <tr><td style="padding:10px;border:1px solid #eee;background:#fafafa;width:100px"><b>商品名</b></td>
          <td style="padding:10px;border:1px solid #eee">{product_name}</td></tr>
      <tr><td style="padding:10px;border:1px solid #eee;background:#fafafa"><b>ASIN</b></td>
          <td style="padding:10px;border:1px solid #eee;font-family:monospace">{asin}</td></tr>
      <tr><td style="padding:10px;border:1px solid #eee;background:#fafafa"><b>検出時刻</b></td>
          <td style="padding:10px;border:1px solid #eee">{now}</td></tr>
    </table>
    <a href="{url}" style="background:#e47911;color:white;padding:14px 28px;text-decoration:none;
       border-radius:6px;display:inline-block;font-weight:bold;font-size:16px">
      🛒 今すぐ購入する →
    </a>
    <p style="color:#aaa;font-size:11px;margin-top:24px;margin-bottom:0">Amazon在庫監視ツールから自動送信</p>
  </div>
</body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = cfg["username"]
    msg["To"]      = cfg["to"]
    msg.attach(MIMEText(body_html, "html", "utf-8"))

    try:
        with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"]) as server:
            server.ehlo()
            server.starttls()
            server.login(cfg["username"], cfg["password"])
            server.sendmail(cfg["username"], cfg["to"], msg.as_string())
        logger.info(f"✉️  メール送信完了 → {cfg['to']}")
    except Exception as e:
        logger.error(f"メール送信失敗: {e}")

# ============================================================
#  非同期チェックループ
# ============================================================

# メモリ上に在庫状態を保持（Railwayはファイルシステムが揮発性のため）
state: dict = {}


async def check_product_async(product: dict, executor: ThreadPoolExecutor):
    asin = product["asin"]
    name = product["name"]
    url  = build_url(asin)

    loop = asyncio.get_running_loop()
    status = await loop.run_in_executor(executor, check_stock_sync, url)

    prev = state.get(asin, {}).get("status", "unknown")
    state.setdefault(asin, {}).update({
        "name":         name,
        "status":       status,
        "last_checked": datetime.now().isoformat(),
        "check_count":  state.get(asin, {}).get("check_count", 0) + 1,
    })

    label = {
        "in_stock":    "✅ 在庫あり",
        "out_of_stock":"❌ 在庫なし",
        "blocked":     "🚫 ブロック中",
        "unknown":     "❓ 不明",
    }.get(status, "❓ 不明")
    logger.info(f"[{name}] {label}")

    if status == "in_stock" and prev == "out_of_stock":
        logger.info(f"🎉 在庫復活: {name} → メール送信")
        await loop.run_in_executor(executor, send_email, name, asin, url)


async def monitor_loop(products: list):
    interval = CONFIG["interval_seconds"]
    jitter   = CONFIG["jitter_seconds"]

    logger.info("=" * 55)
    logger.info("🚀 Amazon 在庫監視ツール（Railway版）起動")
    logger.info(f"   監視商品: {len(products)}件  |  基本間隔: {interval}秒 ±{jitter}秒")
    for p in products:
        logger.info(f"   - {p['name']} ({p['asin']})")
    logger.info("=" * 55)

    cycle = 0
    with ThreadPoolExecutor(max_workers=max(len(products), 4)) as executor:
        while True:
            cycle += 1
            start = time.perf_counter()

            if session_pool.is_blocked:
                remaining = session_pool.block_remaining
                logger.warning(f"🚫 バックオフ中... あと {remaining:.0f}秒")
                await asyncio.sleep(min(remaining, interval))
                continue

            await asyncio.gather(*[
                check_product_async(p, executor) for p in products
            ])

            elapsed   = time.perf_counter() - start
            jitter_val = random.uniform(-jitter, jitter)
            wait       = max(1.0, interval + jitter_val - elapsed)
            logger.info(f"─── サイクル#{cycle} ({elapsed:.2f}秒) | 次回まで {wait:.1f}秒 ───")
            await asyncio.sleep(wait)


def main():
    products_raw = get_products()
    products = []
    for p in products_raw:
        asin = p.get("asin") or extract_asin(p.get("url", ""))
        if not asin:
            logger.warning(f"ASIN取得失敗: {p}")
            continue
        products.append({"asin": asin, "name": p.get("name", f"ASIN:{asin}")})

    if not products:
        logger.error("商品が設定されていません。環境変数 PRODUCTS を設定してください。")
        logger.error('例: [{"asin":"B0CXXXXXXXXX","name":"PS5"}]')
        return

    try:
        asyncio.run(monitor_loop(products))
    except KeyboardInterrupt:
        logger.info("👋 終了しました")


if __name__ == "__main__":
    main()
