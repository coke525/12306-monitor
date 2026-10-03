#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
G7902 余票 / 候补监控器
=====================
车次 G7902：南阳东 -> 焦作，经停 南阳东 / 平顶山西 / 禹州 / 郑州航空港 / 郑州 / 焦作

原理：调用 12306 公开余票查询接口，抓取 G7902 各区间的席位余量，
与上一次结果做对比，一旦某席别从「无」变成有票（或数量增加）就告警。

用法：
    python g7902_monitor.py                 # 执行一次检查并与上次对比
    python g7902_monitor.py --showall       # 打印全部区间明细，不做对比
    python g7902_monitor.py --dates 2026-10-06 --frm PDF --to ZZF

数据源：kyfw.12306.cn（官方公众查询接口，只读，无需登录）
"""

import urllib.request
import http.cookiejar
import ssl
import json
import sys
import os
import time
import argparse
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ---------------------------------------------------------------- 配置区

TRAIN_CODE = "G7902"

DATES = ["2026-10-06", "2026-10-07"]

# G7902 沿途停站顺序（电报码 -> 站名）
STOPS = [
    ("NOF", "南阳东"),
    ("PDF", "平顶山西"),
    ("YZF", "禹州"),
    ("ZIF", "郑州航空港"),
    ("ZZF", "郑州"),
    ("JOF", "焦作"),
]

# 要监控的席别：field index -> 显示名
SEAT_FIELDS = [
    (32, "商务座"),
    (31, "一等座"),
    (30, "二等座"),
    (26, "无座"),
]

# 是否覆盖全部正向区间（推荐）。设 False 则只查 FIXED_PAIRS
MONITOR_ALL_SEGMENTS = True
FIXED_PAIRS = [("PDF", "ZZF"), ("PDF", "JOF")]

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SNAPSHOT = os.path.join(BASE_DIR, "g7902_snapshot.json")
LOGFILE = os.path.join(BASE_DIR, "g7902_log.txt")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

# ---------------------------------------------------------------- 工具函数


def build_opener():
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cj),
        urllib.request.HTTPSHandler(context=CTX),
    )
    op.addheaders = [("User-Agent", UA)]
    return op


def http_get(op, url, referer, timeout=25, retries=3):
    hdrs = [
        ("User-Agent", UA),
        ("Accept", "*/*"),
        ("Accept-Language", "zh-CN,zh;q=0.9"),
        ("Referer", referer),
        ("X-Requested-With", "XMLHttpRequest"),
        ("Sec-Fetch-Dest", "empty"),
        ("Sec-Fetch-Mode", "cors"),
        ("Sec-Fetch-Site", "same-origin"),
    ]
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url)
            for k, v in hdrs:
                req.add_header(k, v)
            return op.open(req, timeout=timeout).read().decode("utf-8", "ignore")
        except Exception as e:      # noqa: BLE001
            last = e
            time.sleep(1.2 * (attempt + 1))
    raise last


def query(op, date, frm, to):
    """查询某一区间的全部车次，返回 result 列表"""
    url = ("https://kyfw.12306.cn/otn/leftTicket/queryG"
           "?leftTicketDTO.train_date={d}&leftTicketDTO.from_station={f}"
           "&leftTicketDTO.to_station={t}&purpose_codes=ADULT").format(d=date, f=frm, t=to)
    raw = http_get(op, url, "https://kyfw.12306.cn/otn/leftTicket/init")
    try:
        j = json.loads(raw)
    except Exception:
        return []
    return ((j.get("data") or {}).get("result")) or []


def parse_train(record):
    """把 | 分隔的记录解析成 dict"""
    p = record.split("|")
    if len(p) < 33:
        return None
    return {
        "code": p[3],
        "from_code": p[6],
        "to_code": p[7],
        "start_time": p[8],
        "arrive_time": p[9],
        "duration": p[10],
        "run_date": p[13],
        "seats": {name: p[idx] for idx, name in SEAT_FIELDS},
    }


# 席位数量判定规则：12306 余票充足时不显示具体数字，而是显示「有」
VALUE_MAP = {"有": 999, "无": 0, "": 0, "-": 0}


def seat_value(v):
    v = (v or "").strip()
    if v.isdigit():
        return int(v)
    return VALUE_MAP.get(v, 0)


def has_ticket(seats):
    """判断是否有可直接下单的余票（非候补）"""
    return any(seat_value(v) > 0 for v in seats.values())


def all_pairs():
    if not MONITOR_ALL_SEGMENTS:
        return list(FIXED_PAIRS)
    codes = [c for c, _ in STOPS]
    out = []
    for i in range(len(codes)):
        for j in range(i + 1, len(codes)):
            out.append((codes[i], codes[j]))
    return out


NAME = dict(STOPS)


def sname(code):
    return NAME.get(code, code)


# ---------------------------------------------------------------- 主流程


def scan(dates, pairs):
    op = build_opener()
    try:
        op.open("https://kyfw.12306.cn/otn/leftTicket/init", timeout=25).read()
    except Exception:
        pass
    stop_codes = {c for c, _ in STOPS}
    rows = {}
    for d in dates:
        for f, t in pairs:
            try:
                res = query(op, d, f, t)
            except Exception as e:      # noqa: BLE001
                log("WARN 查询失败 %s %s->%s : %s" % (d, sname(f), sname(t), e))
                res = []
            for rec in res:
                tr = parse_train(rec)
                if not tr or tr["code"] != TRAIN_CODE:
                    continue
                # 以记录自身携带的真实起讫站为准，避免同城站归并导致的时间错位
                fc, tc = tr["from_code"], tr["to_code"]
                if fc not in stop_codes or tc not in stop_codes:
                    continue
                rows[(d, fc, tc)] = tr
            time.sleep(0.8)
    return rows


def log(line):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    msg = "[%s] %s" % (ts, line)
    print(msg)
    try:
        with open(LOGFILE, "a", encoding="utf-8") as fp:
            fp.write(msg + "\n")
    except Exception:
        pass


def load_snapshot():
    if not os.path.exists(SNAPSHOT):
        return {}
    try:
        with open(SNAPSHOT, "r", encoding="utf-8") as fp:
            return json.load(fp)
    except Exception:
        return {}


def save_snapshot(rows):
    snap = {}
    for (d, f, t), tr in rows.items():
        snap["%s|%s|%s" % (d, f, t)] = tr["seats"]
    try:
        with open(SNAPSHOT, "w", encoding="utf-8") as fp:
            json.dump(snap, fp, ensure_ascii=False, indent=2)
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dates", default=",".join(DATES))
    ap.add_argument("--frm", default=None)
    ap.add_argument("--to", default=None)
    ap.add_argument("--showall", action="store_true")
    args = ap.parse_args()

    dates = [x.strip() for x in args.dates.split(",") if x.strip()]
    pairs = all_pairs() if not (args.frm and args.to) else [(args.frm.upper(), args.to.upper())]

    log("开始检查 %s  日期=%s  区间数=%d" % (TRAIN_CODE, ",".join(dates), len(pairs)))
    rows = scan(dates, pairs)

    if not rows:
        log("未查到 %s 的任何区间数据（可能列车当日停运或接口异常）" % TRAIN_CODE)
        return 1

    prev = load_snapshot()
    alerts = []

    for (d, f, t), tr in sorted(rows.items()):
        key = "%s|%s|%s" % (d, f, t)
        seats = tr["seats"]
        ok = has_ticket(seats)
        tag = "有票" if ok else "无票/仅候补"
        detail = " ".join("%s:%s" % (n, v if v else "-") for n, v in seats.items())
        line = "%s %s->%s %s-%s [%s] %s" % (
            d, sname(f), sname(t), tr["start_time"], tr["arrive_time"], tag, detail)

        if args.showall:
            log("  " + line)
            continue

        old = prev.get(key)
        if old is None:
            if ok:
                alerts.append("新区间且有票 " + line)
        else:
            changed = []
            for n, v in seats.items():
                nv = seat_value(v)
                ov = seat_value(old.get(n))
                if nv > ov:
                    changed.append("%s %s->%s" % (n, (old.get(n) or "无"), v or "无"))
            if changed:
                alerts.append("放票! " + line + "  变化: " + "; ".join(changed))

    if args.showall:
        log("共 %d 个区间" % len(rows))
        return 0

    save_snapshot(rows)

    if alerts:
        log("=== 检测到 %d 条变化 ===" % len(alerts))
        for a in alerts:
            log("  >>> " + a)
    else:
        sellable = sum(1 for tr in rows.values() if has_ticket(tr["seats"]))
        log("本次无变化，共 %d 个区间，其中 %d 个区间有可直接购买的余票" % (len(rows), sellable))
    return 0


if __name__ == "__main__":
    sys.exit(main())
