# -*- coding: utf-8 -*-
"""
監視対象の設定ファイル。設定ツールで自動生成しました。
"""

BASE_URL = "https://k7.p-kashikan.jp/chigasaki-city/"

TARGET_BUILDINGS = {
    "総合体育館": [
        "柔道場",
        "剣道場",
        "オーケストラ練習室",
        "多目的室",
    ],
    "市体育館": [
        "柔剣道場",
        "多目的室",
    ],
}

TARGET_CONDITIONS = [
    {"weekday": "（土）", "hours": ["12", "13", "14"], "label": "土曜 12:00-15:00"},
]

AVAILABLE_MARK = "○"
