"""Invite / share helpers (friend-add URL, LINE share-target link, QR PNG, landing page).

公開してよい情報は「店舗名」と「招待コード」だけ。スタッフ名・時給・userId などは
ここで扱う関数の入力にも出力にも含めない（店舗隔離）。
"""

from __future__ import annotations

import html
import io
import os
import re
from functools import lru_cache
from typing import Any
from urllib.parse import quote

DEFAULT_OA_BASIC_ID = "@813jdxwv"
_CODE_RE = re.compile(r"^[A-Z0-9]{4,12}$")


def oa_basic_id() -> str:
    raw = (os.environ.get("LINE_OA_BASIC_ID") or "").strip()
    if not raw:
        base = (os.environ.get("LINE_INVITE_BASE_URL") or "").strip()
        m = re.search(r"/ti/p/(@?[A-Za-z0-9._-]+)", base)
        raw = m.group(1) if m else DEFAULT_OA_BASIC_ID
    return raw if raw.startswith("@") else "@" + raw


def friend_add_url() -> str:
    """LINE 公式アカウントの友だち追加 URL（店舗データを含まない）。"""
    return f"https://line.me/R/ti/p/{oa_basic_id()}"


def oa_message_url(text: str) -> str:
    """公式アカウントのトークを開き、メッセージを入力済みにする LINE URL スキーム。"""
    return f"https://line.me/R/oaMessage/{quote(oa_basic_id())}/?{quote(text)}"


def line_share_url(text: str) -> str:
    """LINE のシェア（送信先を選ぶ）画面を開く URL。"""
    return "https://line.me/R/share?text=" + quote(text, safe="")


def valid_code(code: str | None) -> str | None:
    c = (code or "").strip().upper()
    return c if _CODE_RE.match(c) else None


def invite_page_path(code: str) -> str:
    return f"/invite/{code}"


def invite_qr_path(code: str) -> str:
    return f"/invite/{code}/qr.png"


def invite_share_text(store_name: str, invite_code: str, *, page_url: str | None = None) -> str:
    """スタッフへ転送する招待文（店舗名＋コード＋友だち追加 URL のみ）。"""
    lines = [
        f"【{store_name}】シフト連絡を LINE で始めます",
        f"① 友だち追加: {friend_add_url()}",
        f"② トークで「登録 {invite_code} お名前」と送信（例: 登録 {invite_code} 太郎）",
        f"招待コード: {invite_code}",
    ]
    if page_url:
        lines.append(f"QR・手順: {page_url}")
    return "\n".join(lines)


def referral_share_text(referral_code: str | None = None) -> str:
    """他の店長へ紹介する文（店舗データは含めない）。"""
    lines = [
        "LINE だけでシフト作成・希望休集め・人件費の見込みまでできるサービスを使っています。",
        "店長さんは友だち追加 →「お店を始める（店長）」で約3分で始められます（14日間無料トライアル）。",
        friend_add_url(),
    ]
    if referral_code:
        lines.append(f"紹介コード: {referral_code}（トークで「紹介コード {referral_code}」と送信）")
    return "\n".join(lines)


def _font(size: int):
    from PIL import ImageFont

    candidates = [
        os.environ.get("LINE_QR_FONT") or "",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    ]
    for p in candidates:
        if p and os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                continue
    return ImageFont.load_default()


@lru_cache(maxsize=256)
def invite_qr_png(store_name: str, invite_code: str) -> bytes:
    """招待 QR（中身は友だち追加 URL）＋店舗名・招待コードを描いた PNG。"""
    import qrcode
    from PIL import Image, ImageDraw

    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=2)
    qr.add_data(friend_add_url())
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    qr_img = qr_img.resize((560, 560), Image.NEAREST)

    width, height = 640, 860
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, width, 92], fill="#06C755")
    title = "スタッフ招待（LINE 友だち追加）"
    f_title, f_name, f_code, f_small = _font(30), _font(32), _font(44), _font(22)

    def center(text: str, y: int, font, fill="#0f172a") -> None:
        w = draw.textlength(text, font=font)
        draw.text(((width - w) / 2, y), text, font=font, fill=fill)

    center(title, 26, f_title, fill="white")
    name = store_name if len(store_name) <= 18 else store_name[:17] + "…"
    center(name, 108, f_name)
    img.paste(qr_img, ((width - 560) // 2, 160))
    center(f"招待コード  {invite_code}", 730, f_code, fill="#065f46")
    center(f"友だち追加後「登録 {invite_code} お名前」と送信", 800, f_small, fill="#475569")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def invite_landing_html(store_name: str, invite_code: str) -> str:
    """モバイル向け招待ページ。店舗名とコード以外は描画しない。"""
    name = html.escape(store_name)
    code = html.escape(invite_code)
    add_url = html.escape(friend_add_url())
    send_url = html.escape(oa_message_url(f"登録 {invite_code} "))
    qr = html.escape(invite_qr_path(invite_code))
    return f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>{name}｜スタッフ招待</title>
<style>
body{{font-family:"Noto Sans CJK JP","Hiragino Sans",system-ui,sans-serif;margin:0;background:#f1f5f9;color:#0f172a}}
header{{background:#06C755;color:#fff;padding:1rem 1.25rem}}
header h1{{margin:0;font-size:1.15rem}}
main{{max-width:440px;margin:0 auto;padding:1rem}}
.card{{background:#fff;border-radius:14px;padding:1.1rem;margin-bottom:1rem;box-shadow:0 1px 3px rgba(0,0,0,.08)}}
.store{{font-size:1.35rem;font-weight:700;margin:.2rem 0 .6rem}}
.code{{font-size:1.9rem;font-weight:700;letter-spacing:.2em;color:#065f46;text-align:center;background:#ecfdf5;border-radius:10px;padding:.5rem}}
.qr{{display:block;margin:.5rem auto;width:100%;max-width:300px}}
.btn{{display:block;text-align:center;background:#06C755;color:#fff;padding:.9rem;border-radius:10px;text-decoration:none;font-weight:700;margin:.6rem 0}}
.btn.sub{{background:#fff;color:#06C755;border:2px solid #06C755}}
ol{{padding-left:1.3rem;line-height:1.8}}
.muted{{color:#64748b;font-size:.8rem}}
</style></head><body>
<header><h1>シフト連絡 LINE へのご招待</h1></header>
<main>
<div class="card">
  <div class="muted">招待元の店舗</div>
  <div class="store">{name}</div>
  <div class="muted">招待コード</div>
  <div class="code">{code}</div>
</div>
<div class="card">
  <a class="btn" href="{add_url}">LINEで友だち追加</a>
  <a class="btn sub" href="{send_url}">登録メッセージを送る（コード入力済み）</a>
  <ol>
    <li>「LINEで友だち追加」をタップ</li>
    <li>「スタッフとして参加」→ 招待コード <b>{code}</b> を送信</li>
    <li>お名前を送れば完了（希望休もボタンで出せます）</li>
  </ol>
</div>
<div class="card">
  <div class="muted">PC の方はスマホの LINE で QR を読み取ってください</div>
  <img class="qr" src="{qr}" alt="友だち追加 QR コード">
</div>
<p class="muted">このページには店舗名と招待コード以外の情報は表示されません。</p>
</main></body></html>"""


def public_https_base(base: str | None) -> str | None:
    b = (base or "").strip().rstrip("/")
    return b if b.startswith("https://") else None


def invite_image_message(base: str | None, code: str) -> dict[str, Any] | None:
    """LINE image message（公開 HTTPS が必要。無ければ None）。"""
    b = public_https_base(base)
    if not b:
        return None
    url = b + invite_qr_path(code)
    return {"type": "image", "originalContentUrl": url, "previewImageUrl": url}
