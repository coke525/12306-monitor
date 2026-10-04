# -*- coding: utf-8 -*-
"""
本地实时余票看板（10-06 / 10-07 平顶山·宝丰 -> 焦作西）

启动：
  python ticket_board.py            # 默认端口 8765
  python ticket_board.py 9000       # 指定端口

浏览器打开 http://127.0.0.1:8765
页面每 30 秒自动刷新；出现「命中出发偏好的完整方案」会响铃 + 顶部红横幅 + 推微信。
后端每 REFRESH_SEC 秒查一次 12306（默认 60 秒，太快会被限流）。
查询逻辑复用 train_monitor.py，避免两份代码跑偏。
"""
import json
import os
import socketserver
import sys
import threading
import time
import urllib.parse
import http.server
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import train_monitor as tm  # noqa: E402
import push  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
REFRESH_SEC = 120   # 路线扩到 22 条×2 天，一轮扫描约 60-80 秒，别低于这个

STATE = {
    "updated": "", "ok": False, "error": "",
    "leg1": [], "leg2": [], "plans": [], "events": [],
}
_LOCK = threading.Lock()
_PREV = {}
_EVENTS = []
_EID = [0]
_PUSHED = set()


def row_obj(k, v):
    d, f, t, code = k
    return {
        "date": d, "code": code,
        "from": tm.sname(f), "to": tm.sname(t),
        "start": v["start"], "arrive": v["arrive"],
        "seats": v["seats"], "total": v["total"],
    }


def scan_once():
    global _PREV
    rows = tm.scan(tm.DATES, tm.LEG1 + tm.LEG2 + tm.LEG3 + tm.EXTRA_WATCH)

    cur = {"%s|%s|%s|%s" % k: v["total"] for k, v in rows.items()}
    new_ev = []
    now = datetime.now().strftime("%H:%M:%S")
    for key, tot in cur.items():
        old = _PREV.get(key, 0)
        if tot > old and tot > 0:
            d, f, t, code = key.split("|")
            _EID[0] += 1
            new_ev.append({
                "id": _EID[0], "time": now, "big": old == 0,
                "text": "%s %s %s→%s 放票（%d→%d）"
                        % (d[5:], code, tm.sname(f), tm.sname(t), old, tot),
            })
        elif tot == 0 and old > 0:
            d, f, t, code = key.split("|")
            _EID[0] += 1
            new_ev.append({
                "id": _EID[0], "time": now, "big": False,
                "text": "%s %s %s→%s 被抢空" % (d[5:], code, tm.sname(f), tm.sname(t)),
            })
    _PREV = cur
    _EVENTS.extend(new_ev)
    del _EVENTS[:-60]

    def tolist(leg):
        return sorted([row_obj(k, v) for k, v in rows.items()
                       if (k[1], k[2]) in leg],
                      key=lambda r: (r["date"], r["start"]))

    leg1 = tolist(tm.LEG1)
    leg2 = tolist(tm.LEG2 + tm.LEG3)
    plans = tm.build_plans(rows, tm.DATES)
    hit = [p for p in plans if p["hit"]]

    # 新出现的命中方案 -> 推微信
    fresh = [p for p in hit if tm.plan_key(p) not in _PUSHED]
    for p in fresh:
        _PUSHED.add(tm.plan_key(p))
        body = ("<p><b>%s</b><br>第一段 %s %s→%s %s-%s（二等 %s / 无座 %s）<br>"
                "第二段 %s %s→%s %s-%s（二等 %s / 无座 %s）<br>"
                "换乘 %d 分钟，预计 %s 到焦作西</p>" % (
                    p["date"], p["a_code"], p["a_from"], p["a_to"],
                    p["a_start"], p["a_arrive"],
                    p["a_seats"]["二等座"] or "-", p["a_seats"]["无座"] or "-",
                    p["b_code"], p["b_from"], p["b_to"],
                    p["b_start"], p["b_arrive"],
                    p["b_seats"]["二等座"] or "-", p["b_seats"]["无座"] or "-",
                    p["buffer"], p["final"]))
        # 极简推送走 GET，URL 有硬长度限制，必须给短文本
        segs = " + ".join("%s %s%s→%s%s" % (l["code"], l["from"], l["start"],
                                            l["to"], l["arrive"]) for l in p["legs"])
        short = ("【12306放票】%s %s %s %s到焦作西\n马上去12306下单！"
                 % (p["date"][5:], segs, p["seat_tag"], p["final"]))
        ok, msg = push.send("12306 有票了！马上下单", body, short=short)
        _EID[0] += 1
        _EVENTS.append({"id": _EID[0], "time": now, "big": True,
                        "text": "微信推送：" + msg})

    with _LOCK:
        STATE.update({
            "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "ok": True, "error": "",
            "leg1": leg1, "leg2": leg2, "plans": plans[:10],
            "events": list(reversed(_EVENTS)),
        })


def loop():
    while True:
        try:
            scan_once()
        except Exception as e:      # noqa: BLE001
            with _LOCK:
                STATE["error"] = "%s: %s" % (type(e).__name__, e)
        time.sleep(REFRESH_SEC)


HTML_PATH = os.path.join(BASE, "board.html")


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = urllib.parse.urlparse(self.path).path
        if p.startswith("/api"):
            with _LOCK:
                body = json.dumps(STATE, ensure_ascii=False).encode("utf-8")
            self._send(body, "application/json; charset=utf-8")
        else:
            try:
                with open(HTML_PATH, "rb") as fh:
                    self._send(fh.read(), "text/html; charset=utf-8")
            except Exception:
                self._send(b"board.html missing", "text/plain")


if __name__ == "__main__":
    threading.Thread(target=loop, daemon=True).start()
    scan_once()
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True
    print("board running: http://127.0.0.1:%d  dates=%s" % (PORT, ",".join(tm.DATES)))
    srv.serve_forever()
