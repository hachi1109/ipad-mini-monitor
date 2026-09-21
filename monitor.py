#!/usr/bin/env python3
"""Apple 日本 整備済製品(iPad)ストアの「iPad mini (A17 Pro)」在庫監視 → Discord / LINE / メール通知

使い方:
  python3 monitor.py            # 3分おきに常時監視(Mac向け)
  python3 monitor.py --once     # 1回だけ確認して終了(GitHub Actions向け)
  python3 monitor.py --test     # Discordにテスト通知を送って終了
  python3 monitor.py --dry-run  # 通知せず、検出結果だけ表示(動作確認用)

通知先(設定したものすべてに送ります。1つ以上が必須, --dry-run 以外):
  Discord : DISCORD_WEBHOOK_URL
  LINE    : LINE_CHANNEL_ACCESS_TOKEN と LINE_USER_ID  (Messaging API)
  メール  : EMAIL_USER (Gmailアドレス) と EMAIL_APP_PASSWORD (アプリパスワード)
            EMAIL_TO (任意。省略時は EMAIL_USER 宛)

その他の環境変数:
  INTERVAL_SECONDS     (任意, 既定 180)
  STATE_FILE           (任意, 既定 seen.json  通知済み商品の記録)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import smtplib
import ssl
import unicodedata
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

STORE_URL = "https://www.apple.com/jp/shop/refurbished/ipad/ipad-mini"
BASE_URL = "https://www.apple.com"

# 検出条件(全角/半角・ハイフン種類の違いは NFKC 正規化で吸収した上で判定)
# 「iPad mini」かつ「A17 Pro」を両方含む商品名にマッチ
TARGET_PATTERNS = [
    re.compile(r"ipad\s*mini", re.I),
    re.compile(r"a17\s*pro", re.I),
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"
    ),
    "Accept-Language": "ja-JP,ja;q=0.9",
}

INTERVAL = int(os.environ.get("INTERVAL_SECONDS", "180"))
STATE_FILE = Path(os.environ.get("STATE_FILE", "seen.json"))
FAIL_ALERT_THRESHOLD = 10  # 連続失敗がこの回数に達したら1回だけ警告通知


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def norm(text: str) -> str:
    """全角括弧・特殊ハイフン等を通常表記へ(例: 'iPad mini（A17 Pro）Wi‑Fi' → 'iPad mini(A17 Pro)Wi-Fi')"""
    return unicodedata.normalize("NFKC", text).strip()


def is_target(name: str) -> bool:
    n = norm(name)
    return all(p.search(n) for p in TARGET_PATTERNS)


def fetch_html() -> str:
    r = requests.get(STORE_URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


def parse_products(html: str) -> list[dict]:
    """ページ内の商品リンク(/shop/product/...)から {id, name, url} を抽出"""
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, dict] = {}
    for a in soup.select('a[href*="/shop/product/"]'):
        href = a.get("href", "")
        m = re.search(r"/shop/product/([^/?#]+)", href)
        if not m:
            continue
        pid = m.group(1)
        name = norm(a.get_text(" ", strip=True))
        url = urljoin(BASE_URL, href)
        # 同一商品が複数リンク(画像リンク等)で出るので、名前が長い方を採用
        if pid not in found or len(name) > len(found[pid]["name"]):
            found[pid] = {"id": pid, "name": name, "url": url}
    return list(found.values())


def load_seen() -> set[str]:
    try:
        return set(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_seen(seen: set[str]) -> None:
    STATE_FILE.write_text(json.dumps(sorted(seen), ensure_ascii=False), encoding="utf-8")


def _env(name: str) -> str:
    """未設定・空文字・前後の空白/改行を吸収して返す"""
    return (os.environ.get(name) or "").strip()


def send_discord(subject: str, body: str) -> None:
    r = requests.post(
        _env("DISCORD_WEBHOOK_URL"),
        json={"content": f"{subject}\n{body}"},
        timeout=15,
    )
    r.raise_for_status()


def send_line(subject: str, body: str) -> None:
    r = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={"Authorization": f"Bearer {_env('LINE_CHANNEL_ACCESS_TOKEN')}"},
        json={
            "to": _env("LINE_USER_ID"),
            "messages": [{"type": "text", "text": f"{subject}\n{body}"[:4900]}],
        },
        timeout=15,
    )
    if not r.ok:
        raise RuntimeError(f"LINE API エラー {r.status_code}: {r.text[:200]}")


def send_email(subject: str, body: str) -> None:
    user = _env("EMAIL_USER")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = _env("EMAIL_TO") or user
    msg.set_content(body)
    with smtplib.SMTP_SSL(
        "smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=30
    ) as s:
        s.login(user, _env("EMAIL_APP_PASSWORD").replace(" ", ""))
        s.send_message(msg)


def configured_channels() -> list[tuple[str, callable]]:
    """環境変数が揃っている通知先だけを返す"""
    ch: list[tuple[str, callable]] = []
    if _env("DISCORD_WEBHOOK_URL"):
        ch.append(("Discord", send_discord))
    if _env("LINE_CHANNEL_ACCESS_TOKEN") and _env("LINE_USER_ID"):
        ch.append(("LINE", send_line))
    if _env("EMAIL_USER") and _env("EMAIL_APP_PASSWORD"):
        ch.append(("メール", send_email))
    return ch


def notify(subject: str, body: str) -> None:
    """設定済みの全通知先に送る。1つでも成功すれば成功扱い(全滅なら例外)"""
    channels = configured_channels()
    if not channels:
        raise RuntimeError(
            "通知先が未設定です(DISCORD_WEBHOOK_URL / LINE_* / EMAIL_* のいずれかを設定してください)"
        )
    ok = 0
    errors: list[str] = []
    for name, fn in channels:
        try:
            fn(subject, body)
            ok += 1
            log(f"  {name} に送信しました")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {e}")
            log(f"  {name} への送信に失敗: {e}")
    if ok == 0:
        raise RuntimeError("すべての通知先への送信に失敗しました → " + " / ".join(errors))


def check_once(dry_run: bool = False) -> None:
    html = fetch_html()
    products = parse_products(html)
    if not products:
        # 商品が0件=HTML構造の変更/ブロック/メンテナンス等の可能性。誤って「在庫なし」扱いにしない
        raise RuntimeError("商品リンクを1件も抽出できませんでした(ページ構造変更・アクセス制限の可能性)")

    hits = [p for p in products if is_target(p["name"])]
    log(f"商品 {len(products)} 件を確認 / 該当 {len(hits)} 件")

    seen = load_seen()
    keep = {p["id"] for p in hits if p["id"] in seen}  # 掲載継続中の通知済み商品
    failure: Exception | None = None
    for p in hits:
        if p["id"] in seen:
            continue
        subject = "【整備済製品】iPad mini (A17 Pro) が入荷しました"
        body = f"{p['name']}\n{p['url']}"
        if dry_run:
            log(f"[dry-run] 通知内容:\n{subject}\n{body}")
            continue
        try:
            notify(subject, body)
            keep.add(p["id"])
            log(f"通知しました: {p['name']}")
        except Exception as e:  # noqa: BLE001
            failure = e  # 通知に失敗した商品は記録せず、次回もう一度通知を試みる
    if not dry_run:
        # 現在掲載中の通知済みのものだけ記録(売り切れて後日再掲載されたら、再度通知される)
        save_seen(keep)
    if failure:
        raise failure


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="1回だけ実行して終了")
    ap.add_argument("--test", action="store_true", help="テスト通知を送って終了")
    ap.add_argument("--dry-run", action="store_true", help="通知せず検出結果だけ表示(1回)")
    args = ap.parse_args()

    if args.test:
        try:
            notify("【テスト】iPad mini 整備済製品モニター", "この通知が届けば、設定は正常です。")
            return 0
        except Exception as e:  # noqa: BLE001
            log(f"エラー: {e}")
            return 1

    if args.dry_run or args.once:
        try:
            check_once(dry_run=args.dry_run)
            return 0
        except Exception as e:  # noqa: BLE001
            log(f"エラー: {e}")
            return 1

    log(f"監視開始: {INTERVAL}秒おき / 対象: {STORE_URL}")
    fails = 0
    alerted = False
    while True:
        try:
            check_once()
            fails, alerted = 0, False
        except Exception as e:  # noqa: BLE001
            fails += 1
            log(f"エラー({fails}回連続): {e}")
            if fails >= FAIL_ALERT_THRESHOLD and not alerted:
                try:
                    notify("【警告】iPad mini モニター", f"監視が{fails}回連続で失敗しています: {e}")
                    alerted = True
                except Exception as e2:  # noqa: BLE001
                    log(f"警告通知にも失敗: {e2}")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
