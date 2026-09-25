# -*- coding: utf-8 -*-
"""
茅ヶ崎市公共施設予約システム(新システム / P-kashikan)の空き状況を確認し、
対象施設・対象曜日・対象時間帯に空きがあればメール通知するスクリプト。

前提:
- ログイン不要な「空き状況の確認」→「期間の空き状況」機能のみを使用
- GitHub Actions から1日2回実行される想定

画面遷移:
トップページ → 「空き状況の確認」→ タブ「期間の空き状況」
 → 施設一覧から建物(総合体育館/市体育館)をクリック
 → 室場一覧から対象施設名をクリック
 → 約40日分の空き状況が1画面で表示される(このサイトは新しいシステムで、
   以前のような複数フレームには分かれていない普通の1ページ構成のため、
   旧バージョンで必要だった「フレームの探し直し」処理は不要)
"""

import os
import re
import smtplib
import ssl
import time
from email.mime.text import MIMEText
from datetime import datetime
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright, Page

from config import (
    BASE_URL,
    TARGET_BUILDINGS,
    TARGET_HOURS,
    AVAILABLE_MARK,
)


def goto_with_retry(page: Page, url: str, attempts: int = 3):
    """トップページを開く処理。サイトが一時的に重い場合があるため、
    失敗したら少し待って再挑戦する。"""
    last_err = None
    for i in range(attempts):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            return
        except Exception as e:
            last_err = e
            print(f"[デバッグ] ページを開くのに失敗(試行{i + 1}/{attempts}): {e}")
            page.wait_for_timeout(3000)
    raise last_err


def click_by_text(page: Page, text: str, exact: bool = True, timeout_ms: int = 20000):
    """画面上のテキストを目印に要素をクリックする。

    同じテキストを持つ要素が複数存在する(レスポンシブ対応で、非表示の
    メニューにも同じリンクが重複して存在する、など)ことがあるため、
    実際に画面に表示されている要素だけを選んでクリックする。"""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        locator = page.get_by_text(text, exact=exact)
        count = locator.count()
        for i in range(count):
            candidate = locator.nth(i)
            try:
                if candidate.is_visible():
                    candidate.click(timeout=3000)
                    page.wait_for_timeout(700)
                    return
            except Exception:
                continue
        page.wait_for_timeout(300)
    raise RuntimeError(f"'{text}' という表示されている要素が見つかりませんでした")


def safe_content(page: Page, retries: int = 10, delay_ms: int = 500) -> str:
    """page.content() が一瞬のタイミングで失敗することがあるため、
    少し待って再試行する。"""
    last_err = None
    for _ in range(retries):
        try:
            return page.content()
        except Exception as e:
            last_err = e
            time.sleep(delay_ms / 1000)
    raise last_err


def navigate_to_facility_period(page: Page, building: str, room_name: str):
    """トップページから、指定した建物・施設の「期間の空き状況」画面まで進める。"""
    goto_with_retry(page, BASE_URL)
    page.wait_for_timeout(1200)

    click_by_text(page, "空き状況の確認", exact=False)
    click_by_text(page, "期間の空き状況", exact=False)
    click_by_text(page, building, exact=True)
    click_by_text(page, room_name, exact=True)

    # 結果テーブルの読み込みが終わるまで少し待ち、簡単に内容を確認する
    page.wait_for_timeout(1200)
    html = safe_content(page)
    if "施設詳細" not in html and "の空き状況" not in html:
        raise RuntimeError("期間の空き状況の結果画面に到達できませんでした")


def _target_range_str() -> str:
    """TARGET_HOURSから、サイトが内部で使っている '12001500' のような
    時間範囲文字列(開始時刻+00+終了時刻+00)を組み立てる。"""
    start_hour = int(TARGET_HOURS[0])
    end_hour = int(TARGET_HOURS[-1]) + 1
    return f"{start_hour:02d}00{end_hour:02d}00"


# 空きがある(クリックして予約できる)マスにだけ付いている onmousedown 属性から、
# そのマスが対象としている時間範囲('12001500' のような8桁の文字列)を取り出す。
# 予約済み(×)のマスにはこの属性自体が存在しないため、これが見つかること自体が
# 「その時間範囲は予約受付中の空きマスとして表示されている」ことを意味する。
CHANGE_STATUS_PATTERN = re.compile(
    r"changeCheckStatus\(\s*'[^']*'\s*,\s*'[^']*'\s*,\s*\d+\s*,\s*'(\d{8})'"
)


def parse_period_table(html: str):
    """「期間の空き状況」画面のHTMLを解析し、日曜日かつ対象時間帯
    (TARGET_HOURSに対応する時間範囲)が予約受付中の空きになっている
    日付を抽出する。

    戻り値: (空きありと判定した日付のリスト, 日曜日として認識した全行の診断情報)
    診断情報は [(日付表記, その行で見つかった空き時間範囲のリスト), ...] の形。
    """
    target_range = _target_range_str()

    row_pattern = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
    first_cell_pattern = re.compile(r"<td[^>]*>(.*?)</td>", re.IGNORECASE | re.DOTALL)
    tag_strip = re.compile(r"<[^>]+>")

    def clean(cell_html: str) -> str:
        return tag_strip.sub("", cell_html).strip()

    found_dates = []
    sunday_debug = []

    for row_html in row_pattern.findall(html):
        first_match = first_cell_pattern.search(row_html)
        if not first_match:
            continue
        first_text = clean(first_match.group(1))

        if "（日）" not in first_text and "(日)" not in first_text:
            continue  # 日曜日以外の行はスキップ

        available_ranges = CHANGE_STATUS_PATTERN.findall(row_html)
        sunday_debug.append((first_text, available_ranges))

        if target_range in available_ranges:
            found_dates.append(first_text)

    return found_dates, sunday_debug


def check_facility(page: Page, building: str, room_name: str, attempts: int = 3):
    """指定した建物・施設1件の空き状況を確認する。"""
    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            navigate_to_facility_period(page, building, room_name)
            html = safe_content(page)
            dates, sunday_debug = parse_period_table(html)
            print(f"[デバッグ] {building}/{room_name} 日曜日ごとの空き時間範囲: {sunday_debug}")
            print(f"[デバッグ] {building}/{room_name} 判定結果(空き日): {dates}")
            time_label = f"{TARGET_HOURS[0]}:00〜{int(TARGET_HOURS[-1]) + 1}:00"
            return [
                f"{date_str} {building} {room_name} {time_label} 空きあり"
                for date_str in dates
            ]
        except Exception as e:
            last_err = e
            print(f"[デバッグ] {building}/{room_name} 試行{attempt}/{attempts} 失敗: {e}")
            if attempt < attempts:
                page.wait_for_timeout(3000)

    raise last_err


def send_mail(subject: str, body: str):
    gmail_user = os.environ["GMAIL_USER"]
    gmail_app_password = os.environ["GMAIL_APP_PASSWORD"]
    to_addr = os.environ["NOTIFY_TO"]

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = gmail_user
    msg["To"] = to_addr

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(gmail_user, gmail_app_password)
        server.sendmail(gmail_user, [to_addr], msg.as_string())


def main():
    all_found = []
    all_errors = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()

        for building, room_names in TARGET_BUILDINGS.items():
            for room_name in room_names:
                try:
                    found = check_facility(page, building, room_name)
                    all_found.extend(found)
                except Exception as e:
                    all_errors.append(
                        f"[エラー] {building}/{room_name} の確認中に問題が発生しました: {e}"
                    )

        browser.close()

    for err in all_errors:
        print(err)

    # 本当に空きが見つかった場合のみメール送信する(エラーだけの時は送らない)
    if all_found:
        body = "以下の日程で空きが見つかりました。\n\n" + "\n".join(all_found)
        now_jst = datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d %H:%M")
        body += f"\n\n確認日時: {now_jst} (JST)\n{BASE_URL}"
        send_mail("【茅ヶ崎市施設予約】空き通知", body)
        print("空きあり。メール送信しました。")
    else:
        print("空きなし。")


if __name__ == "__main__":
    main()
