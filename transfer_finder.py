#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
平顶山西 / 宝丰 -> 焦作西  一次同站换乘方案搜索
================================================

约束：
  - 起点：平顶山西 (PDF) 或 宝丰 (BFF)
  - 终点：焦作西 (JIF)
  - 最多一次同站换乘
  - 首段不接受夜间出发（默认晚于 20:00 开车的一律排除）

思路：
  1. 穷举从起点出发、还有余票的全部车次（对多个候选终点发起查询并去重）
  2. 对每趟候选车拉取全程经停，找出起点之后的所有后续停靠站
  3. 对每个后续站 X，查询 X -> 焦作西 的车次，筛选出发时间 ≥ 到达 X 的时间 + 换乘缓冲
  4. 输出所有可行的换乘组合，按到达焦作西的时间排序

用法：
    python transfer_finder.py
    python transfer_finder.py --date 2026-10-07 --buffer 40
"""

import urllib.request
import urllib.parse
import http.cookiejar
import ssl
import json
import sys
import time
import re
import argparse
from datetime import datetime, timedelta

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

DATE = "2026-10-06"
ORIGINS = ["PDF", "BFF"]
DEST = "JIF"

# 用于穷举起点出发车次的候选终点（覆盖郑渝/郑万/郑焦各方向）
PROBE_DESTS = ["ZZF", "ZAF", "ZIF", "EZF", "JOF", "NYF", "JIF"]

LATEST_FIRST_DEPART = "20:00"     # 首段最晚可接受发车时间
SEAT_FIELDS = [(32, "商务座"), (31, "一等座"), (30, "二等座"), (26, "无座")]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

NAME = {}
REV = {}


def build_opener():
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cj),
        urllib.request.HTTPSHandler(context=CTX))
    op.addheaders = [("User-Agent", UA)]
    return op


OP = build_opener()
try:
    OP.open("https://kyfw.12306.cn/otn/leftTicket/init", timeout=25).read()
except Exception:
    pass


def http_get(url, retries=3):
    hdrs = [
        ("User-Agent", UA), ("Accept", "*/*"),
        ("Accept-Language", "zh-CN,zh;q=0.9"),
        ("Referer", "https://kyfw.12306.cn/otn/leftTicket/init"),
        ("X-Requested-With", "XMLHttpRequest"),
        ("Sec-Fetch-Dest", "empty"), ("Sec-Fetch-Mode", "cors"),
        ("Sec-Fetch-Site", "same-origin"),
    ]
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url)
            for k, v in hdrs:
                req.add_header(k, v)
            return OP.open(req, timeout=30).read().decode("utf-8", "ignore")
        except Exception as e:      # noqa: BLE001
            last = e
            time.sleep(1.0 * (i + 1))
    raise last


def sv(v):
    v = (v or "").strip()
    if v.isdigit():
        return int(v)
    return {"有": 999, "无": 0, "": 0, "-": 0}.get(v, 0)


_cache_q = {}


def query(date, frm, to):
    key = (date, frm, to)
    if key in _cache_q:
        return _cache_q[key]
    url = ("https://kyfw.12306.cn/otn/leftTicket/queryG"
           "?leftTicketDTO.train_date={d}&leftTicketDTO.from_station={f}"
           "&leftTicketDTO.to_station={t}&purpose_codes=ADULT").format(d=date, f=frm, t=to)
    try:
        j = json.loads(http_get(url))
    except Exception as e:      # noqa: BLE001
        print("  ! 查询失败 %s %s->%s : %s" % (date, frm, to, e))
        j = {}
    res = ((j.get("data") or {}).get("result")) or []
    mp = (j.get("data") or {}).get("map") or {}
    NAME.update(mp)
    out = []
    for rec in res:
        p = rec.split("|")
        if len(p) < 33:
            continue
        out.append({
            "train_no": p[2], "code": p[3], "from": p[6], "to": p[7],
            "start": p[8], "arrive": p[9],
            "seats": {n: p[i] for i, n in SEAT_FIELDS},
            "total": sum(sv(p[i]) for i, _ in SEAT_FIELDS),
        })
    time.sleep(0.7)
    _cache_q[key] = out
    return out


_cache_stop = {}


def stops_of(train_no, frm, to, date):
    key = (train_no, frm, to)
    if key in _cache_stop:
        return _cache_stop[key]
    url = ("https://kyfw.12306.cn/otn/czxx/queryByTrainNo?train_no={t}"
           "&from_station_telecode={f}&to_station_telecode={to}"
           "&depart_date={d}").format(t=train_no, f=frm, to=to, d=date)
    try:
        j = json.loads(http_get(url))
    except Exception:
        j = {}
    data = ((j.get("data") or {}).get("data")) or []
    time.sleep(0.5)
    _cache_stop[key] = data
    return data


def load_station_map():
    """一次性拉取全国电报码 -> 站名、站名 -> 电报码 双向映射"""
    url = "https://kyfw.12306.cn/otn/resources/js/framework/station_name.js"
    try:
        blob = http_get(url)
    except Exception:      # noqa: BLE001
        return
    for item in blob.split("@"):
        parts = item.split("|")
        if len(parts) >= 3 and re.fullmatch(r"[A-Z]{3}", parts[2]):
            NAME[parts[2]] = parts[1]
            REV[parts[1]] = parts[2]


def code_of(name):
    """站名 -> 电报码。找不到返回 None（绝不返回中文，避免污染 URL）"""
    return REV.get(name)


def nm(c):
    return NAME.get(c, c)


def mins(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def hhmm(v):
    return "%02d:%02d" % (v // 60 % 24, v % 60)


def seats_str(s):
    return " ".join("%s:%s" % (k, v or "-") for k, v in s.items())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=DATE)
    ap.add_argument("--buffer", type=int, default=25, help="换乘最少间隔分钟")
    args = ap.parse_args()
    date, buf = args.date, args.buffer

    print("=== %s  起点 %s -> 终点 %s  换乘缓冲 %d 分钟 ==="
          % (date, "/".join(ORIGINS), DEST, buf))
    load_station_map()
    print("站名映射已加载：%d 个车站" % len(REV))

    for origin in ORIGINS:
        print("\n########## 从 %s 出发 ##########" % nm(origin))

        # 1) 收集从该起点出发的所有车次（含无票的，便于后面看全貌）
        seen = {}
        for dst in PROBE_DESTS:
            if dst == origin:
                continue
            for t in query(date, origin, dst):
                if t["from"] != origin:
                    continue
                seen[t["train_no"]] = t

        print("共发现 %d 趟始发车次" % len(seen))
        ok = [t for t in seen.values()
              if t["total"] > 0 and t["start"] <= LATEST_FIRST_DEPART]
        print("其中有余票且 %s 前发车的：%d 趟" % (LATEST_FIRST_DEPART, len(ok)))
        for t in sorted(ok, key=lambda x: x["start"]):
            print("   [首段候选] %s %s -> %s(%s)  %s" %
                  (t["code"], t["start"], nm(t["to"]), t["arrive"], seats_str(t["seats"])))

        # 2) 对每趟候选，找后续停靠站
        plans = []
        for t in ok:
            st = stops_of(t["train_no"], t["from"], t["to"], date)
            after = []
            passed = False
            for s in st:
                if not passed:
                    if s.get("station_name") == nm(t["from"]):
                        passed = True
                    continue
                after.append(s)
            for s in after:
                x_code = code_of(s["station_name"])
                if not x_code:
                    continue
                # 注意：查询终点站同样可以作为换乘站，不能排除
                if x_code == DEST:
                    continue
                # 到达 X 的时刻
                arr_x = s.get("arrive_time") or s.get("start_time")
                if not arr_x or arr_x == "----":
                    continue
                if x_code == DEST:
                    plans.append(None)  # 直达已在下面单独处理
                    continue
                for t2 in query(date, x_code, DEST):
                    if t2["from"] != x_code or t2["to"] != DEST:
                        continue
                    if t2["total"] <= 0:
                        continue
                    if mins(t2["start"]) < mins(arr_x) + buf:
                        continue
                    plans.append({
                        "leg1": t, "mid": nm(x_code), "mid_code": x_code,
                        "arr_x": arr_x, "leg2": t2,
                    })

        if not plans:
            print("  -> 未找到可用的一次换乘组合")
            continue

        plans.sort(key=lambda p: p["leg2"]["arrive"])
        print("\n  可行换乘方案（按到达焦作西时间排序）：")
        for p in plans:
            a, b = p["leg1"], p["leg2"]
            wait = mins(b["start"]) - mins(p["arr_x"])
            print("   %s %s -> %s %s   |  %s 换乘 %d 分钟  |  %s %s -> %s %s   [二等 %s 无座 %s]" % (
                a["code"], a["start"], a["arrive"], nm(a["to"]),
                p["mid"], wait,
                b["code"], b["start"], b["arrive"], nm(DEST),
                b["seats"]["二等座"] or "-", b["seats"]["无座"] or "-"))

        # 3) 直达（同一趟车到焦作西）
        direct = [t for t in query(date, origin, DEST)
                  if t["from"] == origin and t["to"] == DEST and t["total"] > 0]
        print("\n  直达车次（无需换乘）：%s" %
              (", ".join("%s %s->%s" % (t["code"], t["start"], t["arrive"]) for t in direct)
               if direct else "无"))


if __name__ == "__main__":
    main()
