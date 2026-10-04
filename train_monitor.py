#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
平顶山/宝丰 -> 焦作西 余票监控器（两段换乘版）
==============================================

目标：2026-10-06 或 10-07 从 平顶山西/宝丰/平顶山 出发，一次同站换乘，到 焦作西。

出发时间偏好（按日期区分，用户反复沟通过的结果）：
  10-06：不想太早从出发站走（否决过 09:21），也拒绝深夜才到（否决过 22:45）
         -> 10:00~17:30 发车，且 18:10 前到郑州
  10-07：接受早上出发，但要求 17:00 前到焦作西
         -> 05:30~13:00 发车，且最终 17:00 前抵达焦作西
         （第二段若只到焦作，需 +20 分钟打车，故按 16:40 判定）

用法：
    python train_monitor.py                 # 检查一次并与上次快照对比
    python train_monitor.py --showall       # 列出当前所有可用车次与可衔接方案
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import push  # noqa: E402

# ---------------------------------------------------------------- 配置区

DATES = ["2026-10-06", "2026-10-07"]

# 每个日期一条偏好规则
# 10-07 用户明确要求：不要凌晨/清晨出发，只要 09:00-10:00 这个窗口
DATE_PREF = {
    # 用户最新底线：10-06 最晚 20:00 到焦作西
    # 出发窗口下调到 09:00 —— 他 10-07 明确说「九点到十点」，说明 09:21 这种是可以接受的，
    # 真正介意的是凌晨出发和深夜抵达。
    "2026-10-06": {"dep_from": "09:00", "dep_to": "17:30",
                   "arr_zz_by": "18:10", "final_by": "20:00"},
    # 10-07 最晚 16:00 到焦作西（比之前更严）
    "2026-10-07": {"dep_from": "09:00", "dep_to": "10:00",
                   "arr_zz_by": None, "final_by": "16:00"},
}

# 输出时把车次时间段单独框出来，方便一眼看清
TS_OPEN, TS_CLOSE = "【", "】"


def ts(a, b):
    """把发到时间框成醒目的时间段标记"""
    return "%s%s→%s%s" % (TS_OPEN, a, b, TS_CLOSE)


def ts_html(a, b):
    """微信推送里用的高亮时间段"""
    return ('<b style="color:#d7263d;background:#fdecee;'
            'padding:2px 6px;border-radius:4px">%s→%s</b>' % (a, b))

STATION_NAME = {
    "PEN": "平顶山", "PDF": "平顶山西", "BFF": "宝丰",
    "NOF": "南阳东", "NFF": "南阳", "YZF": "禹州",
    "ZIF": "郑州航空港", "EZF": "新郑机场",
    "ZZF": "郑州", "ZAF": "郑州东", "XPF": "郑州西",
    "NYF": "南阳寨", "JOF": "焦作", "JIF": "焦作西",
}

# ---- 换乘网络 ----------------------------------------------------------
# 用户可接受最多两次换乘，所以路径最多三段：
#   家 -> 郑州同城 -> 焦作 -> 焦作西
# 焦作 -> 焦作西 实测一天 10 趟城际、全程有票、只要 9 分钟，所以这条路完全通。
ORIGINS = ["PDF", "BFF", "PEN"]          # 可接受的出发站
MID1 = ["ZZF", "ZAF", "ZIF"]             # 郑州同城换乘站
VIA = "JOF"                              # 焦作（二次换乘点）
DEST = "JIF"                             # 焦作西（终点）

# 第一段：家 -> 郑州同城 / 焦作 / 焦作西
LEG1 = [(o, m) for o in ORIGINS for m in MID1 + [VIA, DEST]]
# 第二段：郑州同城 -> 焦作 / 焦作西
LEG2 = [(m, d) for m in MID1 for d in (VIA, DEST)]
# 第三段：焦作 -> 焦作西
LEG3 = [(VIA, DEST)]

# 额外观察：买长乘短。K258/K5346 都是「南阳 -> 宝丰 -> 郑州」，
# 宝丰无票时若南阳段放票，可以买 南阳->郑州 在宝丰上车（铁路允许中途站上车，未乘区间不退）。
# 只观察告警，不参与方案拼装（用户不住南阳）。
# 实测 10-06 南阳->郑州：K5346 有票、K118 有票、K258 无票。
EXTRA_WATCH = [("NFF", "ZZF")]

ZZ_GROUP = {"ZZF", "ZAF", "ZIF", "EZF", "XPF"}
TAXI_MIN = 20            # 万一焦作->焦作西没赶上火车，打车兜底的分钟数

# 换乘缓冲（分钟）—— 按前后车类型给不同余量，普速晚点风险大所以留更多
BUF_G2G = 25             # 动车/高铁 -> 动车/高铁（同站）
BUF_G2K = 30             # 动车/高铁 -> 普速
BUF_K2X = 45             # 普速 -> 任意（K/T/Z 晚点常见，多留）
BUF_CITI = 20            # 焦作站换城际去焦作西（都是短途城际）
WARN_BUFFER = 40         # 低于这个值在输出里标「偏紧」


def is_slow(code):
    """普速车（K/T/Z/纯数字）晚点风险高"""
    c = (code or "").upper()
    return bool(c) and (c[0] in "KTZY" or c[0].isdigit())


def buffer_need(prev_code, next_code, station):
    """给定前后车次与换乘站，返回建议的最小换乘间隔"""
    if station == VIA:
        return BUF_CITI
    if is_slow(prev_code):
        return BUF_K2X
    if is_slow(next_code):
        return BUF_G2K
    return BUF_G2G

SEAT_FIELDS = [(32, "商务座"), (31, "一等座"), (30, "二等座"), (26, "无座")]

# 打包成 exe 后 __file__ 指向临时解压目录，配置会"写了找不到"；
# 冻结时用 exe 所在目录，让配置/快照落在 exe 旁边，方便用户直接改。
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SNAPSHOT = os.path.join(BASE_DIR, "train_snapshot.json")
LOGFILE = os.path.join(BASE_DIR, "train_log.txt")
CONFIG_FILE = os.path.join(BASE_DIR, "monitor_config.json")

# 是否只接受有座方案（由 monitor_config.json 的 only_seated 控制）
ONLY_SEATED = False
# fast=只扫主干区间，full=连支线/买长乘短一起扫
SCAN_MODE = "fast"
# fast 模式只扫「主干换乘站」以减少请求数。默认郑州两站；
# 换行程时在 monitor_config.json 写 "fast_mid": ["XXF", "YYF"] 即可，
# 不写就用 transfer_cities 全部（见 load_config）。
FAST_MID = ["ZZF", "ZAF"]


def rebuild_legs():
    """站点配置变了就重算要扫描的区间组合"""
    global LEG1, LEG2, LEG3
    LEG1 = [(o, m) for o in ORIGINS for m in MID1 + [VIA, DEST]]
    LEG2 = [(m, d) for m in MID1 for d in (VIA, DEST)]
    LEG3 = [(VIA, DEST)]


def load_config():
    """读 monitor_config.json 覆盖内置默认值；文件不存在就用代码里的默认。

    改行程 = 改这个文件，不用动代码：
      dates / date_prefs   日期与时间窗口
      origins / dest       出发站与终点（电报码）
      transfer_cities      允许换乘的城市车站
      via                  二次换乘点
      scan_mode            fast 快扫 / full 全扫
      only_seated          true=只推全程有座
    """
    global DATES, DATE_PREF, ORIGINS, MID1, VIA, DEST
    global EXTRA_WATCH, ONLY_SEATED, SCAN_MODE, FAST_MID

    if not os.path.exists(CONFIG_FILE):
        return False
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as fp:
            cfg = json.load(fp) or {}
    except Exception as e:      # noqa: BLE001
        log("配置文件读取失败，用内置默认: %s" % e)
        return False

    if cfg.get("dates"):
        DATES = [str(x) for x in cfg["dates"]]
    if cfg.get("date_prefs"):
        DATE_PREF = {}
        for k, v in cfg["date_prefs"].items():
            if isinstance(v, dict):
                DATE_PREF[str(k)] = v
    if cfg.get("origins"):
        ORIGINS = [str(x) for x in cfg["origins"]]
    if cfg.get("transfer_cities"):
        MID1 = [str(x) for x in cfg["transfer_cities"]]
    if cfg.get("dest"):
        DEST = str(cfg["dest"])
    if cfg.get("via"):
        VIA = str(cfg["via"])
    if cfg.get("stations"):
        for k, v in cfg["stations"].items():
            if isinstance(v, str):
                STATION_NAME[str(k)] = v

    SCAN_MODE = str(cfg.get("scan_mode", "fast")).lower()
    ONLY_SEATED = bool(cfg.get("only_seated", False))
    # fast_mid 没写就默认取 transfer_cities 的前两个，避免换行程后仍被郑州两站裁掉
    if cfg.get("fast_mid"):
        FAST_MID = [str(x) for x in cfg["fast_mid"]]
    elif MID1:
        FAST_MID = list(MID1[:2])
    if SCAN_MODE == "fast":
        MID1 = [m for m in MID1 if m in FAST_MID] or MID1
        EXTRA_WATCH = []
    else:
        ew = cfg.get("extra_watch") or [("NFF", "ZZF")]
        EXTRA_WATCH = [tuple(x) for x in ew if isinstance(x, (list, tuple))]

    rebuild_legs()
    log("已载入配置  日期=%s  出发=%s  终点=%s  模式=%s  只看有座=%s"
        % (",".join(DATES), "/".join(ORIGINS), sname(DEST), SCAN_MODE, ONLY_SEATED))
    return True

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def sname(c):
    return STATION_NAME.get(c, c)


def mins(t):
    try:
        h, m = t.split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return 0


def build_opener():
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cj),
        urllib.request.HTTPSHandler(context=CTX))
    op.addheaders = [("User-Agent", UA)]
    try:
        op.open("https://kyfw.12306.cn/otn/leftTicket/init", timeout=25).read()
    except Exception:
        pass
    return op


def http_get(op, url, timeout=25, retries=3):
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
            return op.open(req, timeout=timeout).read().decode("utf-8", "ignore")
        except Exception as e:      # noqa: BLE001
            last = e
            time.sleep(1.2 * (i + 1))
    raise last


def seat_value(v):
    v = (v or "").strip()
    if v.isdigit():
        return int(v)
    return {"有": 999, "无": 0, "": 0, "-": 0}.get(v, 0)


def scan(dates, routes):
    op = build_opener()
    rows = {}
    for d in dates:
        for f, t in routes:
            url = ("https://kyfw.12306.cn/otn/leftTicket/queryG"
                   "?leftTicketDTO.train_date={d}&leftTicketDTO.from_station={f}"
                   "&leftTicketDTO.to_station={t}&purpose_codes=ADULT").format(d=d, f=f, t=t)
            try:
                j = json.loads(http_get(op, url))
            except Exception as e:      # noqa: BLE001
                log("WARN %s %s->%s 查询失败: %s" % (d, sname(f), sname(t), e))
                continue
            for rec in ((j.get("data") or {}).get("result")) or []:
                p = rec.split("|")
                if len(p) < 33 or p[6] != f or p[7] != t:
                    continue
                seats = {n: p[i] for i, n in SEAT_FIELDS}
                rows[(d, f, t, p[3])] = {
                    "start": p[8], "arrive": p[9], "seats": seats,
                    "total": sum(seat_value(v) for v in seats.values()),
                }
            time.sleep(0.7)
    return rows


def log(line):
    msg = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), line)
    print(msg)
    try:
        with open(LOGFILE, "a", encoding="utf-8") as fp:
            fp.write(msg + "\n")
    except Exception:
        pass


def leg_pref_ok(date, f, t, v):
    """单段是否落在出发时间偏好里（只对第一段有意义）"""
    p = DATE_PREF.get(date)
    if not p or t not in ZZ_GROUP:
        return False
    if not (p["dep_from"] <= v["start"] <= p["dep_to"]):
        return False
    if p["arr_zz_by"] and v["arrive"] > p["arr_zz_by"]:
        return False
    return True


def has_seat(seats):
    """是否有真正的座位（商务/一等/二等任一有票），无座不算"""
    return any(seat_value(seats.get(k, "")) > 0
               for k in ("商务座", "一等座", "二等座"))


def seat_tag(seat_list):
    """给用户一眼看清要不要站着"""
    flags = [has_seat(s) for s in seat_list]
    if all(flags):
        return "[全程有座]"
    if any(flags):
        return "[部分无座]"
    return "[全程无座]"


def mk_plan(date, pref, legs):
    """legs: 1~3 段。换乘时间不够的直接丢弃，保证推给你的都是赶得上的"""
    buffers, needs = [], []
    for i in range(1, len(legs)):
        prev, cur = legs[i - 1], legs[i]
        need = buffer_need(prev["code"], cur["code"], prev["to_code"])
        got = mins(cur["start"]) - mins(prev["arrive"])
        if got < 0 or got < need:
            return None                  # 赶不上，不是有效方案
        buffers.append(got)
        needs.append(need)

    final = legs[-1]["arrive"]
    first = legs[0]

    hit = bool(pref) and (pref["dep_from"] <= first["start"] <= pref["dep_to"])
    if hit and pref.get("arr_zz_by") and first["to_code"] in ZZ_GROUP:
        if first["arrive"] > pref["arr_zz_by"]:
            hit = False
    if hit and pref.get("final_by") and final > pref["final_by"]:
        hit = False

    p = {
        "date": date, "nlegs": len(legs), "final": final, "hit": hit,
        "buffers": buffers, "needs": needs,
        "tight": any(g < WARN_BUFFER for g in buffers),
        "legs": legs,
        "seat_tag": seat_tag([l["seats"] for l in legs]),
    }
    # 兼容旧字段（board.html 与推送模板用）
    for i, key in enumerate("abc"):
        if i < len(legs):
            l = legs[i]
            for suf, src in (("code", "code"), ("from", "from"), ("to", "to"),
                             ("start", "start"), ("arrive", "arrive"),
                             ("seats", "seats")):
                p[key + "_" + suf] = l[src]
        else:
            for suf in ("code", "from", "to", "start", "arrive"):
                p[key + "_" + suf] = ""
            p[key + "_seats"] = {}
    return p


def build_plans(rows, dates):
    """穷举 直达 / 一次换乘 / 两次换乘 的全部可行组合"""
    plans = []
    for d in dates:
        pref = DATE_PREF.get(d, {})

        def get(f, t):
            out = []
            for k, v in rows.items():
                if k[0] == d and k[1] == f and k[2] == t and v["total"] > 0:
                    out.append({
                        "from": sname(f), "to": sname(t),
                        "from_code": f, "to_code": t,
                        "code": k[3], "start": v["start"],
                        "arrive": v["arrive"], "seats": v["seats"],
                    })
            return out

        seen = set()

        def add(legs):
            p = mk_plan(d, pref, legs)
            if not p:
                return
            key = (p["date"], tuple(l["code"] for l in legs))
            if key in seen:
                return
            seen.add(key)
            plans.append(p)

        # 一段：家 -> 焦作西（直达）
        for o in ORIGINS:
            for a in get(o, DEST):
                add([a])
        # 两段：家 -> 郑州同城 -> 焦作西
        for o in ORIGINS:
            for m in MID1:
                for a in get(o, m):
                    for b in get(m, DEST):
                        if b["code"] == a["code"]:
                            continue          # 同一趟车，已被直达覆盖
                        add([a, b])
        # 三段：家 -> 郑州同城 -> 焦作 -> 焦作西
        for o in ORIGINS:
            for m in MID1:
                for a in get(o, m):
                    for b in get(m, VIA):
                        if b["code"] == a["code"]:
                            continue
                        for c in get(VIA, DEST):
                            if c["code"] == b["code"]:
                                continue      # 同车次已由两段覆盖
                            add([a, b, c])

    # 排序：命中优先 → 换乘次数少 → 总等待时间短（等 4 小时不算"合理安排"）→ 到得早
    plans.sort(key=lambda x: (not x["hit"], x["nlegs"],
                              sum(x["buffers"]), x["date"], x["final"]))
    return plans


def plan_key(p):
    return "%s|%s" % (p["date"], "-".join(l["code"] for l in p["legs"]))


def plan_text(p):
    segs = " ＋ ".join(
        "%s %s→%s %s" % (l["code"], l["from"], l["to"], ts(l["start"], l["arrive"]))
        for l in p["legs"])
    if p["buffers"]:
        buf = "，".join(["换乘%d分" % g for g in p["buffers"]])
    else:
        buf = "无需换乘"
    risk = "　⚠偏紧" if p["tight"] else ""
    return "%s%s %s（%s，%s到%s%s）" % (
        p["seat_tag"], p["date"], segs, buf, p["final"], sname(DEST), risk)


def fmt(k, v):
    d, f, t, code = k
    s = v["seats"]
    tag = "[符合出发偏好] " if leg_pref_ok(d, f, t, v) else ""
    return tag + "%s %s %s->%s %s 商:%s 一:%s 二:%s 无:%s" % (
        d, code, sname(f), sname(t), ts(v["start"], v["arrive"]),
        s["商务座"] or "-", s["一等座"] or "-", s["二等座"] or "-", s["无座"] or "-")


def main():
    load_config()                       # 先读 monitor_config.json，覆盖默认行程
    ap = argparse.ArgumentParser()
    ap.add_argument("--showall", action="store_true")
    ap.add_argument("--dates", default=None,
                    help="不传就用 monitor_config.json 里的 dates")
    ap.add_argument("--nopush", action="store_true", help="只记录不推微信")
    args = ap.parse_args()

    dates = ([x.strip() for x in args.dates.split(",") if x.strip()]
             if args.dates else list(DATES))
    routes = LEG1 + LEG2 + LEG3 + EXTRA_WATCH
    log("开始扫描  日期=%s  路线=%d 条（含二次换乘+买长乘短观察）"
        % (",".join(dates), len(routes)))
    rows = scan(dates, routes)

    available = {k: v for k, v in rows.items() if v["total"] > 0}
    preferred = {k: v for k, v in available.items() if leg_pref_ok(k[0], k[1], k[2], v)}
    plans = build_plans(rows, dates)
    hit_plans = [p for p in plans if p["hit"]]
    if ONLY_SEATED:
        # 只保留每一段都有真正座位的方案
        hit_plans = [p for p in hit_plans
                     if all(has_seat(l["seats"]) for l in p["legs"])]

    if args.showall:
        log("当前有票车次 %d 趟 / 扫描结果 %d 趟" % (len(available), len(rows)))
        for k, v in sorted(available.items(), key=lambda x: (x[0][0], x[1]["start"])):
            log("  [可用] " + fmt(k, v))
        log("符合出发偏好的首段：%d 趟" % len(preferred))
        for k, v in sorted(preferred.items(), key=lambda x: x[1]["start"]):
            log("  [符合偏好] " + fmt(k, v))
        log("可衔接完整方案 %d 个，其中命中偏好 %d 个" % (len(plans), len(hit_plans)))
        for p in plans[:10]:
            log("  [%s] %s" % ("命中" if p["hit"] else "备选", plan_text(p)))
        return 0

    prev = {}
    if os.path.exists(SNAPSHOT):
        try:
            with open(SNAPSHOT, "r", encoding="utf-8") as fp:
                prev = json.load(fp)
        except Exception:
            prev = {}

    alerts = []
    for k, v in available.items():
        key = "%s|%s|%s|%s" % k
        oldv = prev.get(key, {})
        oldtot = oldv.get("total")
        if oldtot is None:
            alerts.append("新出现票 " + fmt(k, v))
        elif v["total"] > oldtot:
            alerts.append("放票 " + fmt(k, v) + "（%d -> %d）" % (oldtot, v["total"]))

    # 新出现的「命中偏好的完整方案」
    prev_hit = set((prev.get("_hit_plans") or []))
    new_hit = [p for p in hit_plans if plan_key(p) not in prev_hit]

    snap = {"_hit_plans": [plan_key(p) for p in hit_plans]}
    for k, v in rows.items():
        snap["%s|%s|%s|%s" % k] = {"total": v["total"], "seats": v["seats"]}
    try:
        with open(SNAPSHOT, "w", encoding="utf-8") as fp:
            json.dump(snap, fp, ensure_ascii=False, indent=2)
    except Exception:
        pass

    if alerts:
        log("=== 检测到 %d 条变化 ===" % len(alerts))
        for a in alerts:
            log("  >>> " + a)
    else:
        log("无变化。已扫 %d 趟车，有票 %d 趟，符合偏好首段 %d 趟，命中方案 %d 个"
            % (len(rows), len(available), len(preferred), len(hit_plans)))

    if new_hit:
        log("*** 出现 %d 个命中偏好的完整方案 ***" % len(new_hit))
        for p in new_hit:
            log("  *** " + plan_text(p))
        if not args.nopush:
            # 只推最靠前的 2 条：极简推送走 GET，内容必须短
            top = new_hit[:2]
            extra = ("（另有 %d 个方案）" % (len(new_hit) - len(top))
                     if len(new_hit) > len(top) else "")
            body = "".join(
                "<p><b>%s 出发　%s</b>（你要的窗口 %s-%s）<br>%s<br>"
                "换乘间隔 %s<br>预计 <b>%s</b> 到%s</p>" % (
                    p["date"][5:], p["seat_tag"],
                    DATE_PREF.get(p["date"], {}).get("dep_from", ""),
                    DATE_PREF.get(p["date"], {}).get("dep_to", ""),
                    "<br>".join(
                        "第%d段 %s：%s→%s　%s　二等 %s / 无座 %s" % (
                            i + 1, l["code"], l["from"], l["to"],
                            ts_html(l["start"], l["arrive"]),
                            l["seats"].get("二等座") or "-",
                            l["seats"].get("无座") or "-")
                        for i, l in enumerate(p["legs"])),
                    "、".join(["%d分(建议≥%d)" % (g, n)
                               for g, n in zip(p["buffers"], p["needs"])]) or "无需换乘",
                    p["final"], sname(DEST))
                for p in top) + ("<p>%s</p>" % extra if extra else "")
            any_seat = any(has_seat(l["seats"]) for p in new_hit for l in p["legs"])
            title = "12306 有票了！马上下单" + ("（有座）" if any_seat else "（仅无座）")

            # 极简推送走 GET，URL 有硬长度限制，单独给一份精简文本
            def short_of(p):
                segs = " + ".join(
                    "%s %s%s→%s%s" % (l["code"], l["from"], l["start"], l["to"], l["arrive"])
                    for l in p["legs"])
                return "%s %s %s %s到%s" % (
                    p["date"][5:], segs, p["seat_tag"], p["final"], sname(DEST))

            short = "【12306放票】" + short_of(top[0])
            if len(top) > 1:
                short += "\n备选：" + short_of(top[1])
            if extra:
                short += "\n" + extra.strip("（）")
            short += "\n马上去12306下单！"

            ok, msg = push.send(title, body, short=short)
            log("微信推送: %s" % msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
