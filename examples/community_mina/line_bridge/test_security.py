#!/usr/bin/env python3
"""/demo/message のなりすまし防止（ローカル直 or 管理トークンのみ）。"""

from __future__ import annotations

import os
import sys
import tempfile
import traceback
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def main() -> int:
    tmp = tempfile.NamedTemporaryFile(prefix="stores_sec_", suffix=".json", delete=False)
    tmp.close()
    os.environ["LINE_STORES_PATH"] = tmp.name
    os.environ["LINE_DEMO_MODE"] = "true"
    os.environ.pop("LINE_DEMO_ENDPOINT", None)
    os.environ.pop("LINE_STORES_ADMIN_TOKEN", None)
    os.environ["LINE_DEMO_ADMIN_TOKEN"] = "t0k-for-tests-only"
    import importlib
    import stores, webhook_app as wh  # noqa: E401
    importlib.reload(stores)
    importlib.reload(wh)
    c = wh.app.test_client()
    body = {"userId": "Uvictim", "text": "メニュー"}

    r = c.post("/demo/message", json=body)
    _assert(r.status_code == 200, f"local direct allowed: {r.status_code}")
    for h in ({"Cf-Connecting-Ip": "203.0.113.9"}, {"X-Forwarded-For": "203.0.113.9"}, {"Cf-Ray": "abc"}):
        r = c.post("/demo/message", json=body, headers=h)
        _assert(r.status_code == 403, f"tunnel/proxy request must be denied: {h} → {r.status_code}")
    r = c.post("/demo/message", json=body, environ_base={"REMOTE_ADDR": "198.51.100.7"})
    _assert(r.status_code == 403, "non-loopback denied")
    r = c.post("/demo/message", json=body, headers={"Cf-Connecting-Ip": "203.0.113.9", "X-Admin-Token": "wrong"})
    _assert(r.status_code == 403, "wrong token denied")
    r = c.post("/demo/message", json=body, headers={"Cf-Connecting-Ip": "203.0.113.9", "X-Admin-Token": "t0k-for-tests-only"})
    _assert(r.status_code == 200, "admin token allowed through tunnel")
    os.environ["LINE_DEMO_ENDPOINT"] = "off"
    _assert(c.post("/demo/message", json=body).status_code == 404, "can be disabled")
    os.environ.pop("LINE_DEMO_ENDPOINT", None)
    # health / invite remain public
    _assert(c.get("/health", headers={"Cf-Ray": "x"}).status_code == 200, "health public")
    print("[sec] ALL ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(1)
