#!/usr/bin/env python3
"""
Amazon 在庫監視ツール - bot回避強化版
設定はすべて環境変数から読み込みます
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
    raw = os.environ.get("PRODUCTS", "[]")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.error('PRODUCTS のJSON形式が正しくありません。例: [{"asin":"B0XXXXXXXXXX","name":"商品名"}]')
        return []

CONFIG = {
    "email": {
        "smtp_host": os.environ.get("SMTP_HOST", "smtp.gmail.com"),
        "smtp_port": int(os.environ.get("SMTP_PORT", "587")),
        "username":  os.environ.get("EMAIL_USER", ""),
        "password":  os.environ.get("EMAIL_PASS", ""),
        "to":        os.environ.get("EMAIL_TO",   ""),
    },
    "interval_seconds":        float(os.environ.get("INTERVAL",       "30")),
    "jitter_seconds":          float(os.environ.get("JITTER",         "10")),
    "captcha_backoff_initial": int(os.environ.get("CAPTCHA_BACKOFF",  "120")),
    "captcha_backoff_max":     int(os.environ.get("CAPTCHA_MAX",      "900")),
    "request_timeout":         int(os.environ.get("REQUEST_TIMEOUT",  "15")),
    # 不明が連続して何回続いたら在庫なしと判定するか
    "unknown_threshold":       int(os.environ.get("UNKNOWN_THRESHOLD", "3")),
}

# ============================================================
#  ロギング
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-5s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("AmazonMonitor")

# ============================================================
#  User-Agent（最新のChromeに合わせる）
# ============================================================

# ブラウザごとにセットで使うヘッダー情報
BROWSER_PROFILES = [
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "sec-ch-ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    },
    {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "sec-ch-ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
    },
    {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
        "sec-ch-ua": '"Firefox";v="124"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    },
]

def build_headers(profile: dict) -> dict:
    """
    本物のChromeが送るヘッダーを再現する。
    sec-ch-ua系はChromeが自動で付けるヘッダーで、
    これがないとbotと判定されやすい。
    """
    return {
        "User-Agent":        profile["User-Agent"],
        "Accept":            "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language":   "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding":   "gzip, deflate, br",
        "sec-ch-ua":         profile["sec-ch-ua"],
        "sec-ch-ua-mobile":  profile["sec-ch-ua-mobile"],
        "sec-ch-ua-platform": profile["sec-ch-ua-platform"],
        "sec-fetch-dest":    "document",
        "sec-fetch-mode":    "navigate",
        "sec-fetch-site":    "none",
        "sec-fetch-user":    "?1",
        "upgrade-insecure-requests": "1",
        "Cache-Control":     "max-age=0",
    }

# ============================================================
#  セッション管理
# ============================================================

class SmartSession:
    """
    人間らしいブラウジングを再現するセッション管理クラス。
    - トップページ訪問でクッキーを取得してから商品ページへ
    - CAPTCHAが出たら指数バックオフで待機
    - ブラウザプロファイルをランダムに切り替え
    """

    def __init__(self):
        self._session = requests.Session()
        self._profile = random.choice(BROWSER_PROFILES)
        self._captcha_backoff = CONFIG["captcha_backoff_initial"]
        self._blocked_until = 0.0
        self._initialized = False  # トップページ訪問済みか

    def _init_session(self):
        """
        最初の1回だけAmazonトップページを訪問してクッキーを取得する。
        人間はいきなり商品ページを開かない、という動きを再現。
        """
        if self._initialized:
            return
        try:
            logger.info("セッション初期化中（トップページ訪問）...")
            self._session.get(
                "https://www.amazon.co.jp",
                headers=build_headers(self._profile),
                timeout=CONFIG["request_timeout"],
            )
            # 人間らしく少し待つ
            time.sleep(random.uniform(1.5, 3.0))
            self._initialized = True
            logger.info("セッション初期化完了")
        except Exception as e:
            logger.warning(f"セッション初期化失敗（続行します）: {e}")
            self._initialized = True  # 失敗しても続行

    def rotate_profile(self):
        """ブラウザプロファイルとセッションを丸ごと切り替える"""
        self._profile = random.choice(BROWSER_PROFILES)
        self._session = requests.Session()
        self._initialized = False
        logger.info("ブラウザプロファイルを切り替えました")

    def report_captcha(self):
        wait = min(self._captcha_backoff, CONFIG["captcha_backoff_max"])
        self._blocked_until = time.time() + wait
        logger.warning(f"⚠️  CAPTCHA検出。{wait}秒待機後にプロファイル切り替えます...")
        self._captcha_backoff = min(self._captcha_backoff * 2, CONFIG["captcha_backoff_max"])

    def reset_backoff(self):
        self._captcha_backoff = CONFIG["captcha_backoff_initial"]

    @property
    def is_blocked(self):
        return time.time() < self._blocked_until

    @property
    def block_remaining(self):
        return max(0.0, self._blocked_until - time.time())

    def get(self, url: str) -> requests.Response:
        self._init_session()
        headers = build_headers(self._profile)
        return self._session.get(url, headers=headers, timeout=CONFIG["request_timeout"])


smart_session = SmartSession()

# ============================================================
#  在庫チェック
# ============================================================

IN_STOCK_SIGNALS     = ["カートに入れる", "今すぐ購入", "Add to Cart", "add-to-cart-button", "In Stock"]
OUT_OF_STOCK_SIGNALS = ["現在在庫切れです", "在庫切れ", "Currently unavailable",
                        "currently-unavailable", "この商品は現在お取り扱いできません"]

def extract_asin(value: str):
    value = value.strip()
    if re.match(r"^[A-Z0-9]{10}$", value, re.IGNORECASE):
        return value.upper()
    for pattern in [r"/dp/([A-Z0-9]{10})", r"/gp/product/([A-Z0-9]{10})", r"asin=([A-Z0-9]{10})"]:
        m = re.search(pattern, value, re.IGNORECASE)
        if m:
            return m.group(1).upper()
    return None

def build_urls(asin: str) -> list:
    """
    複数のURLパターンを返す。
    Amazonはページの構造が商品によって違うため、
    複数パターンを試すことで取得精度が上がる。
    """
    return [
        f"https://www.amazon.co.jp/dp/{asin}",
        f"https://www.amazon.co.jp/gp/product/{asin}",
    ]

def parse_stock(html: str) -> str:
    """
    HTMLから在庫状態を判定する。
    より厳密に判定するため、複数の方法を組み合わせる。
    """
    soup = BeautifulSoup(html, "lxml")

    # 方法1: availability div（最も信頼性が高い）
    avail = soup.find(id="availability")
    if avail:
        text = avail.get_text(" ", strip=True)
        logger.debug(f"availability テキスト: {text}")
        if any(s in text for s in ["在庫あり", "In Stock", "通常", "入荷予定", "残り"]):
            return "in_stock"
        if any(s in text for s in ["在庫切れ", "Currently unavailable",
                                    "現在在庫切れ", "お取り扱いできません", "販売中止"]):
            return "out_of_stock"

    # 方法2: カートボタンの存在
    if soup.find(id="add-to-cart-button"):
        return "in_stock"

    # 方法3: 今すぐ購入ボタン
    if soup.find(id="buy-now-button"):
        return "in_stock"

    # 方法4: フォールバック文字列検索
    if any(s in html for s in OUT_OF_STOCK_SIGNALS):
        return "out_of_stock"
    if any(s in html for s in IN_STOCK_SIGNALS):
        return "in_stock"

    return "unknown"

def check_stock_sync(asin: str) -> str:
    """
    在庫チェックのメイン処理。
    複数URLを試して判定する。
    """
    if smart_session.is_blocked:
        return "blocked"

    urls = build_urls(asin)

    for url in urls:
        try:
            resp = smart_session.get(url)
            resp.raise_for_status()
            html = resp.text

            # CAPTCHA検出
            if "captcha" in html.lower() or "Type the characters" in html:
                smart_session.report_captcha()
                return "blocked"

            # ページが正常に取得できたか確認
            # （Amazonがブロックした場合は正常なHTMLが返らない）
            if "amazon.co.jp" not in html and len(html) < 5000:
                logger.warning(f"不正なレスポンス（{len(html)}文字）: {url}")
                continue

            smart_session.reset_backoff()
            status = parse_stock(html)

            # unknownの場合は次のURLを試す
            if status != "unknown":
                return status

        except requests.Timeout:
            logger.debug(f"タイムアウト: {url}")
            continue
        except requests.RequestException as e:
            logger.debug(f"リクエストエラー: {e}")
            continue

    return "unknown"

# ============================================================
#  メール通知
# ============================================================

def send_email(product_name: str, asin: str, url: str):
    cfg = CONFIG["email"]
    if not cfg["username"] or not cfg["password"]:
        logger.warning("メール設定が未入力のため通知をスキップします")
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

state: dict = {}
# 連続unknown回数を商品ごとに記録
unknown_counts: dict = {}

async def check_product_async(product: dict, executor: ThreadPoolExecutor):
    asin = product["asin"]
    name = product["name"]
    url  = f"https://www.amazon.co.jp/dp/{asin}"

    loop = asyncio.get_running_loop()
    status = await loop.run_in_executor(executor, check_stock_sync, asin)

    # unknown が threshold 回続いたら out_of_stock と判定
    # （Amazonのブロックによる誤判定を防ぐ）
    if status == "unknown":
        unknown_counts[asin] = unknown_counts.get(asin, 0) + 1
        threshold = CONFIG["unknown_threshold"]
        if unknown_counts[asin] >= threshold:
            logger.warning(f"[{name}] ❓不明が{threshold}回続いたため在庫なしと判定")
            status = "out_of_stock"
    else:
        unknown_counts[asin] = 0

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

    # 在庫復活通知（out_of_stock → in_stock のときだけ）
    if status == "in_stock" and prev == "out_of_stock":
        logger.info(f"🎉 在庫復活: {name} → メール送信")
        await loop.run_in_executor(executor, send_email, name, asin, url)


async def monitor_loop(products: list):
    interval = CONFIG["interval_seconds"]
    jitter   = CONFIG["jitter_seconds"]
    cycle    = 0
    # 何サイクルごとにプロファイルを切り替えるか
    ROTATE_EVERY = 20

    logger.info("=" * 55)
    logger.info("🚀 Amazon 在庫監視ツール（bot回避強化版）起動")
    logger.info(f"   監視商品: {len(products)}件  |  基本間隔: {interval}秒 ±{jitter}秒")
    for p in products:
        logger.info(f"   - {p['name']} ({p['asin']})")
    logger.info("=" * 55)

    with ThreadPoolExecutor(max_workers=max(len(products), 4)) as executor:
        while True:
            cycle += 1

            # 定期的にブラウザプロファイルを切り替える
            if cycle % ROTATE_EVERY == 0:
                smart_session.rotate_profile()

            if smart_session.is_blocked:
                remaining = smart_session.block_remaining
                logger.warning(f"🚫 バックオフ中... あと {remaining:.0f}秒")
                await asyncio.sleep(min(remaining, interval))
                # バックオフ明けはプロファイルを切り替える
                smart_session.rotate_profile()
                continue

            start = time.perf_counter()

            # 商品を並列チェック
            await asyncio.gather(*[
                check_product_async(p, executor) for p in products
            ])

            elapsed    = time.perf_counter() - start
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
