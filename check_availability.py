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


def parse_period_table(html: str):
    """「期間の空き状況」画面のHTMLを解析し、日曜日かつ対象時間帯
    (TARGET_HOURSの列すべて)が○になっている日付を抽出する。

    この画面は1週間ごとに見出し行(「施設」+ 時刻)が繰り返される作りに
    なっている。また、同じ状態が続く時間帯は colspan で1つのマスに
    まとめて表示されることがあるため、単純に「何番目のセルか」では
    正しい時刻位置を特定できない。そのため colspan を考慮して、
    各セルが実際にどの時刻の列をカバーしているかを計算する。

    戻り値: (空きありと判定した日付のリスト, 日曜日として認識した全行の診断情報)
    """
    row_pattern = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
    cell_full_pattern = re.compile(r"<(td|th)([^>]*)>(.*?)</\1>", re.IGNORECASE | re.DOTALL)
    colspan_pattern = re.compile(r'colspan\s*=\s*"?(\d+)"?', re.IGNORECASE)
    tag_strip = re.compile(r"<[^>]+>")

    def clean(cell_html: str) -> str:
        return tag_strip.sub("", cell_html).strip()

    def parse_cells(row_html):
        """行の中の各セルを (テキスト, colspan) のリストで返す。"""
        cells = []
        for _, attrs, inner in cell_full_pattern.findall(row_html):
            m = colspan_pattern.search(attrs)
            span = int(m.group(1)) if m else 1
            cells.append((clean(inner), span))
        return cells

    hour_col_index = {}  # 例: {"12": 4, "13": 5, "14": 6}
    found_dates = []
    sunday_debug = []
    header_seen_count = 0

    for row_html in row_pattern.findall(html):
        cells = parse_cells(row_html)
        if not cells:
            continue

        first_text = cells[0][0]

        if first_text == "施設":
            # 見出し行: 各時刻(8,9,10...)が絶対列位置の何番目かを記録する
            header_seen_count += 1
            hour_col_index = {}
            col_cursor = 0
            for text, span in cells[1:]:
                if text in TARGET_HOURS and text not in hour_col_index:
                    hour_col_index[text] = col_cursor
                col_cursor += span
            continue

        if not hour_col_index:
            continue

        if "（日）" not in first_text and "(日)" not in first_text:
            continue

        # 日付行: colspanを考慮して、各絶対列位置の値を組み立てる
        value_by_col = {}
        col_cursor = 0
        for text, span in cells[1:]:
            for c in range(col_cursor, col_cursor + span):
                value_by_col[c] = text
            col_cursor += span

        marks = [value_by_col.get(hour_col_index.get(h)) for h in TARGET_HOURS]
        sunday_debug.append((first_text, marks))
        if all(m == AVAILABLE_MARK for m in marks):
            found_dates.append(first_text)

    if header_seen_count == 0:
        sunday_debug.append(("(見出し行「施設」が1つも見つかりませんでした)", []))

    return found_dates, sunday_debug


def check_facility(page: Page, building: str, room_name: str, attempts: int = 3):
    """指定した建物・施設1件の空き状況を確認する。"""
    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            navigate_to_facility_period(page, building, room_name)
            html = safe_content(page)
            dates, sunday_debug = parse_period_table(html)
            print(f"[デバッグ] {building}/{room_name} 日曜日の生データ(対象={TARGET_HOURS}): {sunday_debug}")
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
        body += f"\n\n確認日時: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n{BASE_URL}"
        send_mail("【茅ヶ崎市施設予約】空き通知", body)
        print("空きあり。メール送信しました。")
    else:
        print("空きなし。")


if __name__ == "__main__":
    main()
