"""LINE Messaging API 向けの薄いブリッジ（店舗シフト PoC）。

トークン未設定時はデモ／モックモードで動作します。
実 LINE への送受信はユーザーが Channel secret / token を設定した場合のみ。
"""

from .shift_messages import (
    build_shift_flex,
    build_shift_text,
    connection_status,
    parse_user_intent,
    run_shift_for_line,
)

__all__ = [
    "build_shift_flex",
    "build_shift_text",
    "connection_status",
    "parse_user_intent",
    "run_shift_for_line",
]
