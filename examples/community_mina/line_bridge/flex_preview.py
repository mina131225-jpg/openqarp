#!/usr/bin/env python3
"""LINE Flex Message JSON → 簡易 HTML プレビュー（スクリーンショット用・開発ツール）。

usage: python flex_preview.py message.json out.html
"""

from __future__ import annotations

import html
import json
import sys
from typing import Any

SIZES = {"xxs": 11, "xs": 13, "sm": 14, "md": 16, "lg": 19, "xl": 22}


def _text(c: dict[str, Any]) -> str:
    style = [f"font-size:{SIZES.get(c.get('size', 'md'), 16)}px", f"color:{c.get('color', '#111')}", f"flex:{c.get('flex', 1)}"]
    if c.get("weight") == "bold":
        style.append("font-weight:700")
    if c.get("align") == "end":
        style.append("text-align:right")
    if c.get("margin"):
        style.append("margin-top:8px")
    return f"<div style='{';'.join(style)};white-space:pre-wrap;word-break:break-word'>{html.escape(c.get('text', ''))}</div>"


def render(c: dict[str, Any]) -> str:
    t = c.get("type")
    if t == "text":
        return _text(c)
    if t == "separator":
        return "<hr style='border:0;border-top:1px solid #e2e8f0;margin:10px 0'>"
    if t == "button":
        a = c.get("action") or {}
        primary = c.get("style") == "primary"
        bg = "#06C755" if primary else ("transparent" if c.get("style") == "link" else "#eef2f7")
        fg = "#fff" if primary else "#1e293b"
        return (f"<div style='background:{bg};color:{fg};border-radius:8px;padding:9px;text-align:center;"
                f"font-weight:600;font-size:14px;margin-top:6px'>{html.escape(a.get('label', ''))}</div>")
    if t == "box":
        horiz = c.get("layout") == "horizontal"
        pad = c.get("paddingAll") or "0px"
        bg = c.get("backgroundColor") or "transparent"
        inner = "".join(render(x) for x in c.get("contents") or [])
        disp = "display:flex;gap:8px;align-items:flex-start" if horiz else ""
        mt = "margin-top:6px;" if c.get("margin") else ""
        return f"<div style='{disp};{mt}padding:{pad};background:{bg}'>{inner}</div>"
    return ""


def bubble(b: dict[str, Any]) -> str:
    parts = [render(b[k]) for k in ("header", "body", "footer") if b.get(k)]
    width = {"giga": 420, "mega": 340, "kilo": 280}.get(b.get("size", "mega"), 340)
    return (f"<div style='width:{width}px;background:#fff;border-radius:14px;overflow:hidden;"
            f"box-shadow:0 1px 4px rgba(0,0,0,.15);flex:none'>{''.join(parts)}</div>")


def page(msg: dict[str, Any]) -> str:
    c = msg.get("contents") or msg
    bubbles = c.get("contents") if c.get("type") == "carousel" else [c]
    qr = "".join(
        f"<span style='display:inline-block;border:1px solid #06C755;color:#06C755;border-radius:16px;"
        f"padding:4px 10px;margin:3px;font-size:13px;background:#fff'>{html.escape(i['action']['label'])}</span>"
        for i in (msg.get("quickReply") or {}).get("items") or []
    )
    return ("<!doctype html><html lang='ja'><meta charset='utf-8'><body style='margin:0;background:#8cabd9;"
            "font-family:\"Noto Sans CJK JP\",sans-serif;padding:16px'>"
            f"<div style='display:flex;gap:10px'>{''.join(bubble(b) for b in bubbles)}</div>"
            f"<div style='margin-top:10px'>{qr}</div></body></html>")


if __name__ == "__main__":
    data = json.load(open(sys.argv[1], encoding="utf-8"))
    open(sys.argv[2], "w", encoding="utf-8").write(page(data))
