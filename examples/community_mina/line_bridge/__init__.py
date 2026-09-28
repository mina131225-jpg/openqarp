"""LINE Messaging API 向けの薄いブリッジ（店舗シフト PoC・マルチテナント）。

販売時は顧客の LINE 公式アカウントを使う想定。スタッフは友だち追加後に
「登録 <店舗コード>」で店舗へ紐付く。開発者個人 LINE は不要。

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
from .stores import (
    create_store,
    get_store_for_user,
    list_stores,
    mask_user_id,
    register_user,
)

__all__ = [
    "build_shift_flex",
    "build_shift_text",
    "connection_status",
    "create_store",
    "get_store_for_user",
    "list_stores",
    "mask_user_id",
    "parse_user_intent",
    "register_user",
    "run_shift_for_line",
]
