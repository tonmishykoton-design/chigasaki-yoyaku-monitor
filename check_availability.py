# -*- coding: utf-8 -*-
"""
茅ヶ崎市公共施設予約サービスの空き状況を確認し、
対象施設・対象曜日・対象時間帯に空きがあればメール通知するスクリプト。

前提:
- ログイン不要な「空き状況の確認」機能のみを使用
- GitHub Actions から1日2回実行される想定

設計上の注意(重要):
このサイトはリンクをクリックすると、フレームの中身だけでなく
フレームそのものが作り直される(古いFrameオブジェクトが「detached」に
なる)ことがある。そのため、一度取得したFrameオブジェクトを使い回さず、
「次に何かする直前に、毎回そのつどページ全体から目的のフレームを
探し直す」という設計にしている。
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


def find_frame_with_selector(page: Page, selector: str, timeout_ms: int = 20000) -> Frame:
    """指定したセレクタ(リンクのテキストなど)にマッチする要素を持つフレームを、
    ページの全フレームの中から探す。見つかるまでリトライする。
    毎回ページの最新のフレーム一覧から探すため、古いフレームが
    detached(消滅)していても影響を受けない。"""
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        for frame in page.frames:
            try:
                if frame.locator(selector).count() > 0:
                    return frame
            except Exception:
                continue
        page.wait_for_timeout(300)
    return None


def dump_frames_for_debug(page: Page, label: str):
    print(f"[デバッグ] '{label}' が見つかりませんでした。現在のフレーム一覧:")
    for f in page.frames:
        try:
            print(f"  - url={f.url}")
        except Exception as e:
            print(f"  - url取得失敗: {e}")


def click_text_link(page: Page, text: str, timeout_ms: int = 20000):
    """リンクのテキストが存在するフレームを毎回探し直してクリックする。"""
    selector = f"a:has-text('{text}')"
    frame = find_frame_with_selector(page, selector, timeout_ms=timeout_ms)
    if frame is None:
        dump_frames_for_debug(page, text)
        raise RuntimeError(f"リンク '{text}' を含むフレームが見つかりませんでした")

    frame.locator(selector).first.click(timeout=timeout_ms)
    try:
        frame.wait_for_load_state("domcontentloaded", timeout=10000)
    except Exception:
        pass
    page.wait_for_timeout(700)


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


def navigate_to_result_table(page: Page, building: str):
    """トップページから、指定した建物の「開始時間指定(空き状況一覧)」画面まで進める。
    (Frameオブジェクトは返さない。以降は毎回そのつど探し直す)"""
    goto_with_retry(page, BASE_URL)
    page.wait_for_timeout(1500)  # フレーム内コンテンツの読み込みを待つ

    click_text_link(page, "空き状況の確認")
    click_text_link(page, "屋内（体育施設）")
    click_text_link(page, building)
    # 第一条件選択画面: 目的選択タブがデフォルトで開いている想定。
    # 「屋内その他」を選べば建物内の全施設が一覧表示される。
    click_text_link(page, "屋内その他")


def safe_content(frame: Frame, retries: int = 15, delay_ms: int = 500) -> str:
    """frame.content() はページ遷移の一瞬とタイミングが重なると
    失敗することがあるため、少し待って再試行する。"""
    last_err = None
    for _ in range(retries):
        try:
            return frame.content()
        except Exception as e:
            last_err = e
            time.sleep(delay_ms / 1000)
    raise last_err


def get_result_frame(page: Page, timeout_ms: int = 20000) -> Frame:
    """空き状況の一覧表(または「次の日」「一週間後」リンク)を含む
    フレームを、その都度ページ全体から探す。"""
    selector = "a:has-text('次の日'), a:has-text('一週間後'), table"
    frame = find_frame_with_selector(page, selector, timeout_ms=timeout_ms)
    if frame is None:
        dump_frames_for_debug(page, "結果テーブル")
        raise RuntimeError("結果テーブルを含むフレームが見つかりませんでした")
    return frame


def parse_table_for_targets(html: str, facility_keywords):
    """開始時間指定ページのHTML文字列を読み取り、対象施設の対象時間帯が
    空き(○)かどうかを判定する。(HTML文字列に対する軽量パースのみ行い、
    Playwrightのライブオブジェクトには依存しない)"""
    date_match = re.search(r"(令和\d+年\d+月\d+日)", html)
    date_str = date_match.group(1) if date_match else "(日付不明)"
    is_sunday = "(日)" in html or "（日）" in html

    results = []

    # <tr> ... </tr> を1行ずつ抜き出す(改行含む場合があるためDOTALL)
    row_pattern = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
    cell_pattern = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)
    tag_strip = re.compile(r"<[^>]+>")

    def clean(cell_html: str) -> str:
        return tag_strip.sub("", cell_html).strip()

    rows = row_pattern.findall(html)
    if not rows:
        return results, date_str, is_sunday

    header_cells = [clean(c) for c in cell_pattern.findall(rows[0])]
    col_index = None
    for i, text in enumerate(header_cells):
        if TARGET_TIME_COLUMN in text:
            col_index = i
            break

    if col_index is None:
        return results, date_str, is_sunday

    for row_html in rows[1:]:
        cells = [clean(c) for c in cell_pattern.findall(row_html)]
        if len(cells) <= col_index:
            continue
        facility_name = cells[0]
        if not any(kw in facility_name for kw in facility_keywords):
            continue
        mark = cells[col_index]
        results.append((facility_name, mark == AVAILABLE_MARK))

    return results, date_str, is_sunday


def advance(page: Page, link_text: str) -> bool:
    """「次の日」または「一週間後」のリンクを、そのつどフレームを
    探し直してクリックする。リンクが無ければFalseを返す。"""
    selector = f"a:has-text('{link_text}')"
    frame = find_frame_with_selector(page, selector, timeout_ms=5000)
    if frame is None:
        return False
    frame.locator(selector).first.click()
    try:
        frame.wait_for_load_state("domcontentloaded", timeout=10000)
    except Exception:
        pass
    page.wait_for_timeout(700)
    return True


def check_building(page: Page, building: str, facility_keywords, attempts: int = 3):
    """指定した建物の空き状況を確認する。

    古いサイト特有の一過性の遅延・タイミングのズレで失敗することがあるため、
    失敗した場合は最初からやり直す(最大 attempts 回)。
    """
    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            navigate_to_result_table(page, building)

            # 日曜日になるまで「次の日」を押す(最大7回で必ず到達する)
            for _ in range(7):
                frame = get_result_frame(page)
                html = safe_content(frame)
                if "(日)" in html or "（日）" in html:
                    break
                if not advance(page, "次の日"):
                    break

            found = []
            for _ in range(MAX_WEEKS_AHEAD):
                frame = get_result_frame(page)
                html = safe_content(frame)
                results, date_str, is_sunday = parse_table_for_targets(html, facility_keywords)
                if is_sunday:
                    for facility_name, available in results:
                        if available:
                            found.append(
                                f"{date_str}（日） {facility_name} {TARGET_TIME_COLUMN}〜 空きあり"
                            )
                if not advance(page, "一週間後"):
                    break  # サイト側の検索可能期間の終端に到達

            return found  # 成功したらここで終了

        except Exception as e:
            last_err = e
            print(f"[デバッグ] {building} の確認 試行{attempt}/{attempts} 失敗: {e}")
            if attempt < attempts:
                page.wait_for_timeout(4000)  # 少し間を空けてから最初からやり直す

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

        for building, keywords in TARGET_BUILDINGS.items():
            try:
                found = check_building(page, building, keywords)
                all_found.extend(found)
            except Exception as e:
                # サイトが夜間閉鎖されている時間帯や、一時的な不調で
                # 発生することがあるため、これは異常終了とはせず
                # ログにのみ残す(「空きあり」メールとは混同しない)。
                all_errors.append(f"[エラー] {building} の確認中に問題が発生しました: {e}")

        browser.close()

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
