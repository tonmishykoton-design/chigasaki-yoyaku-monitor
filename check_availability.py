# -*- coding: utf-8 -*-
"""
茅ヶ崎市公共施設予約サービスの空き状況を確認し、
対象施設・対象曜日・対象時間帯に空きがあればメール通知するスクリプト。

前提:
- ログイン不要な「空き状況の確認」機能のみを使用
- GitHub Actions から1日2回(朝9時・夜11時)実行される想定
"""

import os
import re
import smtplib
import ssl
import time
from email.mime.text import MIMEText
from datetime import datetime

from playwright.sync_api import sync_playwright, Page, Frame

from config import (
    BASE_URL,
    TARGET_BUILDINGS,
    TARGET_TIME_COLUMN,
    MAX_WEEKS_AHEAD,
    AVAILABLE_MARK,
)


def find_frame_with_link(page: Page, text: str, timeout_ms: int = 15000):
    """クリックしたいリンクのテキストが実際に存在するフレームを、
    全フレームの中から探す。見つかるまで一定時間リトライする。

    (このサイトは複数のフレームに分かれており、共通メニュー部分にも
    サイト名など紛らわしい文字列が含まれるため、「らしきフレームを一つ
    推測する」方式ではなく、目的のリンクそのものを直接探す方式にしている)
    """
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        for frame in page.frames:
            try:
                locator = frame.locator(f"a:has-text('{text}')")
                if locator.count() > 0:
                    return frame, locator
            except Exception:
                continue
        page.wait_for_timeout(300)
    return None, None


def dump_frames_for_debug(page: Page, text: str):
    print(f"[デバッグ] '{text}' を含むフレームが見つかりませんでした。現在のフレーム一覧:")
    for f in page.frames:
        try:
            content = safe_content(f, retries=2, delay_ms=300)
            print(f"  - url={f.url} content_len={len(content)}")
        except Exception as e:
            print(f"  - url={f.url} content取得失敗: {e}")


def click_link(page: Page, text: str, timeout: int = 20000) -> Frame:
    """リンクのテキストが存在するフレームを直接探してクリックし、
    そのフレームを返す。"""
    frame, locator = find_frame_with_link(page, text, timeout_ms=timeout)
    if frame is None:
        dump_frames_for_debug(page, text)
        raise RuntimeError(f"リンク '{text}' を含むフレームが見つかりませんでした")

    locator.first.click(timeout=timeout)
    try:
        frame.wait_for_load_state("domcontentloaded", timeout=10000)
    except Exception:
        pass
    page.wait_for_timeout(500)
    return frame


def navigate_to_result_table(page: Page, building: str) -> Frame:
    """トップページから、指定した建物の「開始時間指定(空き状況一覧)」画面まで進める。"""
    page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(1500)  # フレーム内コンテンツの読み込みを待つ

    click_link(page, "空き状況の確認")
    click_link(page, "屋内（体育施設）")
    click_link(page, building)
    # 第一条件選択画面: 目的選択タブがデフォルトで開いている想定。
    # 「屋内その他」を選べば建物内の全施設が一覧表示される。
    frame = click_link(page, "屋内その他")
    return frame


def safe_content(frame: Frame, retries: int = 6, delay_ms: int = 400) -> str:
    """frame.content() はページ遷移の一瞬とタイミングが重なると
    失敗することがあるため、少し待って再試行する。"""
    last_err = None
    for _ in range(retries):
        try:
            return frame.content()
        except Exception as e:
            last_err = e
            frame.page.wait_for_timeout(delay_ms)
    raise last_err


def parse_table_for_targets(frame: Frame, facility_keywords):
    """開始時間指定ページの表を読み取り、対象施設の対象時間帯が
    空き(○)かどうかを判定する。"""
    html = safe_content(frame)

    date_match = re.search(r"(令和\d+年\d+月\d+日)", html)
    date_str = date_match.group(1) if date_match else "(日付不明)"
    is_sunday = "(日)" in html or "（日）" in html

    results = []
    rows = frame.locator("table tr")
    row_count = rows.count()
    if row_count == 0:
        return results, date_str, is_sunday

    header_cells = rows.nth(0).locator("th, td")
    col_index = None
    for i in range(header_cells.count()):
        text = header_cells.nth(i).inner_text().strip()
        if TARGET_TIME_COLUMN in text:
            col_index = i
            break

    if col_index is None:
        return results, date_str, is_sunday

    for r in range(1, row_count):
        row = rows.nth(r)
        cells = row.locator("td")
        if cells.count() <= col_index:
            continue
        facility_name = cells.nth(0).inner_text().strip()
        if not any(kw in facility_name for kw in facility_keywords):
            continue
        mark = cells.nth(col_index).inner_text().strip()
        results.append((facility_name, mark == AVAILABLE_MARK))

    return results, date_str, is_sunday


def advance_to_next_week(frame: Frame) -> bool:
    locator = frame.locator("a:has-text('一週間後')")
    if locator.count() == 0:
        return False
    locator.first.click()
    try:
        frame.wait_for_load_state("domcontentloaded", timeout=10000)
    except Exception:
        pass
    frame.page.wait_for_timeout(500)
    return True


def advance_to_next_day(frame: Frame) -> bool:
    locator = frame.locator("a:has-text('次の日')")
    if locator.count() == 0:
        return False
    locator.first.click()
    try:
        frame.wait_for_load_state("domcontentloaded", timeout=10000)
    except Exception:
        pass
    frame.page.wait_for_timeout(500)
    return True


def check_building(page: Page, building: str, facility_keywords):
    frame = navigate_to_result_table(page, building)
    found = []

    # 日曜日になるまで「次の日」を押す(最大7回で必ず到達する)
    for _ in range(7):
        html = safe_content(frame)
        if "(日)" in html or "（日）" in html:
            break
        if not advance_to_next_day(frame):
            break

    for _ in range(MAX_WEEKS_AHEAD):
        results, date_str, is_sunday = parse_table_for_targets(frame, facility_keywords)
        if is_sunday:
            for facility_name, available in results:
                if available:
                    found.append(
                        f"{date_str}（日） {facility_name} {TARGET_TIME_COLUMN}〜 空きあり"
                    )
        if not advance_to_next_week(frame):
            break  # サイト側の検索可能期間の終端に到達

    return found


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

        for building, keywords in TARGET_BUILDINGS.items():
            try:
                found = check_building(page, building, keywords)
                all_found.extend(found)
            except Exception as e:
                # サイトが夜間(23:00〜9:00)閉鎖されている時間帯に実行されると
                # ここでタイムアウトする。これは異常ではないので、
                # 「空きあり」メールとは混同せず、ログにのみ残す。
                all_errors.append(f"[エラー] {building} の確認中に問題が発生しました: {e}")

        browser.close()

    # エラーはログに出すだけ(メールでは通知しない)。
    # サイトの夜間閉鎖時間帯にあたっただけの可能性が高いため。
    for err in all_errors:
        print(err)

    # 本当に空きが見つかった場合のみメール送信する
    if all_found:
        body = "以下の日程で空きが見つかりました。\n\n" + "\n".join(all_found)
        body += f"\n\n確認日時: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n{BASE_URL}"
        send_mail("【茅ヶ崎市施設予約】空き通知", body)
        print("空きあり。メール送信しました。")
    else:
        print("空きなし。")


if __name__ == "__main__":
    main()
