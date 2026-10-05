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
# 采用「分层」模型，路线只能从低层流向高层，中间层可以整层跳过：
#
#   层0 出发站  ->  层1 一次换乘  ->  层2 二次换乘  ->  层3 三次换乘  ->  层终点
#
# 能走几段 = 层数-1，所以想支持几次换乘就配几层：
#   只配 层1            -> 最多 1 次换乘（2 段）
#   配 层1+层2          -> 最多 2 次换乘（3 段）
#   配 层1+层2+层3      -> 最多 3 次换乘（4 段）
# 每层都可以填多个站，程序会自动穷举所有组合（含跳过中间层的直达走法）。
ORIGINS = ["PDF", "BFF", "PEN"]          # 层0：可接受的出发站
MID1 = ["ZZF", "ZAF", "ZIF"]             # 层1：一次换乘站
VIA = ["JOF"]                            # 层2：二次换乘站（可多个）
VIA2 = []                                # 层3：三次换乘站（留空=最多两次换乘）
DEST = "JIF"                             # 终点：焦作西
# 同行人模式：一群人先坐同一趟车到 split_at，下车后各走各的。
# monitor_config.json 里写（可以写任意多个，每个人的终点和最晚到达都能不一样）：
#   "companions": [
#     {"name":"小明", "dest":"LYF", "split_at":["ZAF","ZZF"],
#      "tickets":2, "final_by":"18:00"},
#     {"name":"小红", "dest":"XXF", "split_at":"ZZF",
#      "tickets":2, "final_by":null}      ← null = 跟主终点用同一套底线
#   ]
# 不配这个字段 = 完全不启用，行为和以前一模一样。
COMPANIONS = []      # 每个元素：name / dest / split_at(list) / tickets / final_by
MAX_TRANSFERS = 3                        # 结果里最多展示几次换乘（<=2 可提速）

# 扫描用路线集合，由 rebuild_legs() 自动生成（兼容旧名的三段拆分）
LEG1, LEG2, LEG3 = [], [], []
ROUTES = []

# 额外观察：买长乘短。K258/K5346 都是「南阳 -> 宝丰 -> 郑州」，
# 宝丰无票时若南阳段放票，可以买 南阳->郑州 在宝丰上车（铁路允许中途站上车，未乘区间不退）。
# 只观察告警，不参与方案拼装（用户不住南阳）。
# 实测 10-06 南阳->郑州：K5346 有票、K118 有票、K258 无票。
EXTRA_WATCH = [("NFF", "ZZF")]

ZZ_GROUP = {"ZZF", "ZAF", "ZIF", "EZF", "XPF"}
TAXI_MIN = 20            # 万一焦作->焦作西没赶上火车，打车兜底的分钟数
# 短途城际之间换乘只需 20 分钟（默认=所有二次换乘站，可用 city_stations 覆盖）
CITI_STATIONS = set()

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
    if station in CITI_STATIONS:
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
MAX_ROUTES = 70                       # 超过这个数就告警：一轮扫描会明显变慢
SCAN_MODE = "fast"
# fast 模式只扫「主干换乘站」以减少请求数。默认郑州两站；
# 换行程时在 monitor_config.json 写 "fast_mid": ["XXF", "YYF"] 即可，
# 不写就用 transfer_cities 全部（见 load_config）。
FAST_MID = ["ZZF", "ZAF"]


def _dests():
    """终点集合：主终点 + 每个同行人的终点（没配同行人就只有一个）"""
    out = [str(DEST).strip().upper()]
    for c in COMPANIONS:
        d = str(c.get("dest", "")).strip().upper()
        if d and d not in out:
            out.append(d)
    return out


def _layers():
    """按层号从低到高返回换乘网络，最后一层固定是终点站（可能不止一个）。

    空层会被丢掉（比如不配 VIA2 就没有第三次换乘），
    剩下的层数直接决定「最多几次换乘」。
    """
    layers = [[str(x) for x in ORIGINS] if not isinstance(ORIGINS, str)
              else [ORIGINS]]
    for lay in (MID1, VIA, VIA2):
        nodes = [str(x).strip().upper() for x in (lay or []) if str(x).strip()]
        seen, clean = set(), []
        for n in nodes:
            if n not in seen:
                seen.add(n)
                clean.append(n)
        if clean:
            layers.append(clean)
    dests = _dests()
    layers.append(list(dests))
    # 去重：中途出现的终点站会被后面的直连绕开，先剔除以免绕圈
    for lay in layers[:-1]:
        for i in range(len(lay) - 1, -1, -1):
            if lay[i] in dests:
                del lay[i]
    return [lay for lay in layers if lay] or [list(dests)]


def max_segs():
    """当前网络最多能拼几段（含首尾），并受 MAX_TRANSFERS 约束"""
    return min(len(_layers()) - 1, int(MAX_TRANSFERS) + 1)


def rebuild_legs():
    """站点配置变了就重算要扫描的区间组合。

    边 = 任意两层之间的所有节点配对（低层 -> 高层），
    中间层能否跳过由穷举阶段决定，扫描阶段先把边全扫回来。
    """
    global LEG1, LEG2, LEG3, ROUTES
    layers = _layers()
    edges, seen = [], set()
    for i in range(len(layers) - 1):
        for j in range(i + 1, len(layers)):
            for a in layers[i]:
                for b in layers[j]:
                    if a == b or (a, b) in seen:
                        continue
                    seen.add((a, b))
                    edges.append((a, b))
    ROUTES = edges
    # LEG1/2/3 只用于兼容旧代码（按出发层分组），三者相加 == ROUTES
    LEG1 = [e for e in edges if any(e[0] in layers[k] for k in (0,))]
    LEG2 = [e for e in edges if len(layers) > 1 and e[0] in layers[1]]
    LEG3 = [e for e in edges if e not in LEG1 and e not in LEG2]


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
    global DATES, DATE_PREF, ORIGINS, MID1, VIA, VIA2, DEST
    global EXTRA_WATCH, ONLY_SEATED, SCAN_MODE, FAST_MID
    global MAX_TRANSFERS, CITI_STATIONS, COMPANIONS

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
    # 同行人：一群人共乘到 split_at 分手，之后各走各的。没配就是单人模式
    def as_split(v):
        """分手站可以填一个（'ZZF'）也可以填多个（['ZZF','ZAF']）"""
        if isinstance(v, (list, tuple)):
            return [str(x).strip().upper() for x in v if str(x).strip()]
        v = str(v or "").strip().upper()
        return [x.strip() for x in v.replace("，", ",").split(",") if x.strip()]

    def one_companion(comp):
        """解析单个同行人。dest 为空/null 表示这条没启用，返回 None"""
        if not isinstance(comp, dict):
            return None
        # 注意 dest 写成 null 或留空都要当成「不启用」，不能转成字符串 "NONE"
        dest = comp.get("dest")
        if not dest or not str(dest).strip():
            return None
        return {
            "dest": str(dest).strip().upper(),
            "split_at": as_split(comp.get("split_at")),
            "name": str(comp.get("name", "同伴")).strip() or "同伴",
            "tickets": int(comp.get("tickets", 2) or 2),
            "final_by": (str(comp["final_by"]).strip()
                         if comp.get("final_by") else ""),
        }

    raw = cfg.get("companions")
    if raw is None:                      # 兼容只有一个人时的旧写法
        raw = cfg.get("companion")
    if isinstance(raw, dict):            # 兼容 "companion": {...}
        raw = [raw]
    COMPANIONS = []
    if isinstance(raw, (list, tuple)):
        for i, c in enumerate(raw):
            one = one_companion(c)
            if one:
                if not one["split_at"]:
                    log("WARN 同行人「%s」没填分手站，已跳过" % one["name"])
                    continue
                if one["dest"] == str(DEST).strip().upper():
                    log("WARN 同行人「%s」的终点和主终点一样，已跳过" % one["name"])
                    continue
                COMPANIONS.append(one)

    def as_list(v):
        """配置里既可以写单个字符串 'JOF'，也可以写列表 ['JOF','XXF']"""
        if not v:
            return []
        if isinstance(v, str):
            return [x.strip().upper() for x in v.replace("，", ",").split(",")
                    if x.strip()]
        return [str(x).strip().upper() for x in v if str(x).strip()]

    if cfg.get("via"):
        VIA = as_list(cfg["via"])
    if cfg.get("via2"):
        VIA2 = as_list(cfg["via2"])
    if cfg.get("max_transfers") is not None:
        try:
            MAX_TRANSFERS = max(1, int(cfg["max_transfers"]))
        except (TypeError, ValueError):
            pass
    if cfg.get("city_stations"):
        CITI_STATIONS = set(as_list(cfg["city_stations"]))
    else:
        CITI_STATIONS = set(VIA) | set(VIA2)
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
        # 组合会爆炸，fast 下每层只保留少量主干站，防止一轮扫描拖到几分钟
        MID1 = [m for m in MID1 if m in FAST_MID] or MID1
        VIA = VIA[:2]
        VIA2 = []
        EXTRA_WATCH = []
    else:
        ew = cfg.get("extra_watch") or [("NFF", "ZZF")]
        EXTRA_WATCH = [tuple(x) for x in ew if isinstance(x, (list, tuple))]

    rebuild_legs()
    log("已载入配置  日期=%s  出发=%s  终点=%s  模式=%s  只看有座=%s"
        % (",".join(DATES), "/".join(ORIGINS),
           "/".join(sname(x) for x in _dests()), SCAN_MODE, ONLY_SEATED))
    for c in COMPANIONS:
        log("同行人  %s 去 %s（最晚%s到），两人在 %s 分手，共同段需 ≥%d 张票"
            % (c["name"], sname(c["dest"]), c["final_by"] or "不限",
               "/".join(sname(x) for x in c["split_at"]), c["tickets"]))
    log("换乘网络  %s → %s   最多 %d 次换乘，需扫描 %d 条区间"
        % (" → ".join("/".join(x) for x in _layers()),
           sname(DEST), max_segs() - 1, len(ROUTES)))
    if len(ROUTES) > MAX_ROUTES:
        log("WARN 扫描区间过多（%d 条 > %d），一轮会很慢，"
            "建议减少出发站/换乘站，或保持 scan_mode=fast"
            % (len(ROUTES), MAX_ROUTES))
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


def final_by_for(dest, date):
    """按终点取「最晚到达」——每个人的目的地不同，到达底线也可以不同。

    同行人自己填了 final_by 就用他的；没填（null/留空）就沿用当天主终点的底线。
    """
    for c in COMPANIONS:
        if c["dest"] == dest and c.get("final_by"):
            return c["final_by"]
    return (DATE_PREF.get(date) or {}).get("final_by")


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
    pref = dict(pref or {})
    # 终点不同，到达底线可以不同：同行人单独设了就用他的，没设沿用当天主终点
    fb = final_by_for(legs[-1]["to_code"], date)
    if fb:
        pref["final_by"] = fb

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
    """穷举换乘网络里的全部可行路径。

    层数决定上限：N 层最多 N-1 段（即 N-2 次换乘），中间层可整层跳过，
    所以「直达 / 一次换乘 / 两次换乘 / 三次换乘」都在同一个 DFS 里自然产生。
    """
    plans = []
    layers = _layers()
    nlayer = len(layers)
    seg_cap = max_segs()
    dests = set(_dests())

    for d in dates:
        pref = DATE_PREF.get(d, {})
        cache = {}

        def get(f, t):
            """某天某区间所有有票的车次（结果缓存，DFS 里会反复问）"""
            if (f, t) in cache:
                return cache[(f, t)]
            out = []
            for k, v in rows.items():
                if k[0] == d and k[1] == f and k[2] == t and v["total"] > 0:
                    out.append({
                        "from": sname(f), "to": sname(t),
                        "from_code": f, "to_code": t,
                        "code": k[3], "start": v["start"],
                        "arrive": v["arrive"], "seats": v["seats"],
                    })
            cache[(f, t)] = out
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

        def walk(li, node, legs):
            """当前停在 layer[li] 的 node 站，legs 是已走路径"""
            if node in dests:
                add(legs)
                return
            if len(legs) >= seg_cap:
                return
            used = {l["code"] for l in legs}
            for nj in range(li + 1, nlayer):
                for nxt in layers[nj]:
                    for leg in get(node, nxt):
                        if leg["code"] in used:
                            continue          # 同一趟车不停两次，交给下层去重
                        walk(nj, nxt, legs + [leg])

        for o in layers[0]:
            walk(0, o, [])

    # 排序：命中优先 → 换乘次数少 → 总等待时间短（等 4 小时不算"合理安排"）→ 到得早
    plans.sort(key=lambda x: (not x["hit"], x["nlegs"],
                              sum(x["buffers"]), x["date"], x["final"]))
    return plans


def plan_key(p):
    return "%s|%s" % (p["date"], "-".join(l["code"] for l in p["legs"]))


def enough_for(seats, n):
    """这一趟车能不能一次买到 n 张（同一席位等级，保证两人坐一起）"""
    vals = [seat_value(v) for v in (seats or {}).values()]
    return max(vals or [0]) >= n


def shared_legs(p, splits):
    """方案里两人「一起坐」的那几段：从第一段到第一次抵达任一分手站为止。

    压根不经过分手站就返回 None，说明这条路线没法跟对方同行。
    """
    splits = set(splits or [])
    for i, l in enumerate(p["legs"]):
        if l["to_code"] in splits:
            return p["legs"][:i + 1]
    return None


def pair_one(plans, comp):
    """把「我」和某一个同行人的方案按共用段配成同行方案。

    严格规则：共用段的车次与区间必须逐段一致（即同一趟车、同一区间），
    并且共用段每一段的余票都要够所有人，否则不算真的能同行。
    """
    if not comp.get("split_at"):
        return []
    mine_dest = str(DEST).strip().upper()
    fr_dest = comp["dest"]
    need = max(1, int(comp.get("tickets", 2) or 2))

    buckets = {}
    for p in plans:
        dest = p["legs"][-1]["to_code"]
        if dest not in (mine_dest, fr_dest):
            continue
        shared = shared_legs(p, comp["split_at"])
        if not shared:
            continue
        key = (p["date"], tuple((l["code"], l["from_code"], l["to_code"])
                                for l in shared))
        b = buckets.setdefault(key, {})
        b["mine" if dest == mine_dest else "friend"] = p

    out = []
    for (_date, _k), b in buckets.items():
        mine, fri = b.get("mine"), b.get("friend")
        if not mine or not fri:
            continue                       # 只有一个人走通，不算同行方案
        shared = shared_legs(mine, comp["split_at"])
        ok = all(enough_for(l["seats"], need) for l in shared)
        out.append({
            "date": mine["date"], "shared": shared, "need": need,
            "mine": mine, "friend": fri, "tickets_ok": ok, "comp": comp,
            "hit": bool(mine["hit"] and fri["hit"] and ok),
            "final": max(mine["final"], fri["final"]),
        })
    return out


def pair_companions(plans):
    """对每个同行人分别配对，返回所有「可同行」组合。

    多个人时「我」可能同时和好几个人配得上，每种组合都会单独列出来。
    """
    out = []
    for comp in COMPANIONS:
        out.extend(pair_one(plans, comp))
    out.sort(key=lambda x: (not x["hit"], x["date"], x["final"]))
    return out


def pair_key(x):
    return "PAIR|%s|%s|%s|%s" % (x["comp"]["name"], x["date"],
                                 plan_key(x["mine"]), plan_key(x["friend"]))


def pair_text(x):
    def seg(ls):
        return " ＋ ".join("%s %s→%s %s" % (
            l["code"], l["from"], l["to"], ts(l["start"], l["arrive"]))
            for l in ls)

    n = len(x["shared"])

    def tail(p):
        """分手之后各自怎么走；分手站就是终点的话直接下车"""
        rest = p["legs"][n:]
        if not rest:
            return "在%s下车（%s到）" % (p["legs"][-1]["to"], p["final"])
        return "%s 到%s（%s到）" % (seg(rest), p["legs"][-1]["to"], p["final"])

    warn = "" if x["tickets_ok"] else "　⚠共同段余票不足%d张" % x["need"]
    return "[同行] %s 一起坐 %s 到%s；之后你 %s，%s %s%s" % (
        x["date"], seg(x["shared"]), x["shared"][-1]["to"],
        tail(x["mine"]), x["comp"]["name"], tail(x["friend"]), warn)


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
        p["seat_tag"], p["date"], segs, buf, p["final"],
        sname(p["legs"][-1]["to_code"]), risk)


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
    routes = ROUTES + EXTRA_WATCH
    if not routes:
        rebuild_legs()
        routes = ROUTES + EXTRA_WATCH
    log("开始扫描  日期=%s  路线=%d 条（含买长乘短观察）"
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
        if COMPANIONS:
            pairs_all = pair_companions(plans)
            log("可同行方案 %d 个，其中命中偏好 %d 个"
                % (len(pairs_all), len([x for x in pairs_all if x["hit"]])))
            for x in pairs_all[:10]:
                log("  [%s] %s" % ("同行" if x["hit"] else "备选",
                                   pair_text(x)))
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
    pairs = pair_companions(plans)
    hit_pairs = [x for x in pairs if x["hit"]]
    prev_hit_pairs = set(prev.get("_hit_pairs") or [])
    new_pairs = [x for x in hit_pairs if pair_key(x) not in prev_hit_pairs]

    # 已经配成同行方案的就不再单推「我」那一条，免得同一趟车提醒两次
    paired_keys = set()
    for x in hit_pairs:
        paired_keys.add(plan_key(x["mine"]))
        paired_keys.add(plan_key(x["friend"]))
    new_hit = [p for p in hit_plans
               if plan_key(p) not in prev_hit and plan_key(p) not in paired_keys]

    snap = {"_hit_plans": [plan_key(p) for p in hit_plans],
            "_hit_pairs": [pair_key(x) for x in hit_pairs]}
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

    if new_pairs:
        who = {}
        for x in new_pairs:
            who[x["comp"]["name"]] = who.get(x["comp"]["name"], 0) + 1
        log("*** 出现 %d 个可同行方案：%s ***"
            % (len(new_pairs), "、".join("%s %d 个" % (k, v)
                                        for k, v in who.items())))
        for x in new_pairs:
            log("  *** " + pair_text(x))
    if new_hit:
        log("*** 出现 %d 个命中偏好的完整方案（仅你一人可走）***" % len(new_hit))
        for p in new_hit:
            log("  *** " + plan_text(p))

    if (new_pairs or new_hit) and not args.nopush:
        # 只推最靠前的几条：极简推送走 GET，内容必须短
        top_pairs, top_solo = new_pairs[:2], new_hit[:2]
        rest = (len(new_pairs) - len(top_pairs)) + (len(new_hit) - len(top_solo))
        extra = "（另有 %d 个方案）" % rest if rest > 0 else ""

        def legs_html(ls, start=1):
            return "<br>".join(
                "第%d段 %s：%s→%s　%s　二等 %s / 无座 %s" % (
                    start + i, l["code"], l["from"], l["to"],
                    ts_html(l["start"], l["arrive"]),
                    l["seats"].get("二等座") or "-",
                    l["seats"].get("无座") or "-")
                for i, l in enumerate(ls)) or "无需再换乘"

        def seg_short(ls):
            return " + ".join("%s %s%s→%s%s" % (
                l["code"], l["from"], l["start"], l["to"], l["arrive"])
                for l in ls)

        body = ""
        for x in top_pairs:
            n = len(x["shared"])
            body += ("<p><b>[可与%s同行] %s 出发</b><br>共同乘坐：%s<br>"
                     "你接续：%s（<b>%s</b> 到%s）<br>%s接续：%s（<b>%s</b> 到%s）%s</p>") % (
                x["comp"]["name"], x["date"][5:], legs_html(x["shared"]),
                legs_html(x["mine"]["legs"][n:], n + 1),
                x["mine"]["final"], sname(x["mine"]["legs"][-1]["to_code"]),
                x["comp"]["name"],
                legs_html(x["friend"]["legs"][n:], n + 1),
                x["friend"]["final"], sname(x["friend"]["legs"][-1]["to_code"]),
                "" if x["tickets_ok"] else "<br>⚠共同段余票不足%d张" % x["need"])
        for p in top_solo:
            body += (
                "<p><b>%s 出发　%s</b>（你要的窗口 %s-%s）<br>%s<br>"
                "换乘间隔 %s<br>预计 <b>%s</b> 到%s</p>" % (
                    p["date"][5:], p["seat_tag"],
                    DATE_PREF.get(p["date"], {}).get("dep_from", ""),
                    DATE_PREF.get(p["date"], {}).get("dep_to", ""),
                    legs_html(p["legs"]),
                    "、".join(["%d分(建议≥%d)" % (g, n)
                               for g, n in zip(p["buffers"], p["needs"])]) or "无需换乘",
                    p["final"], sname(p["legs"][-1]["to_code"])))
        if extra:
            body += "<p>%s</p>" % extra

        seat_src = ([l for p in new_hit for l in p["legs"]] +
                    [l for x in new_pairs for l in x["mine"]["legs"]] +
                    [l for x in new_pairs for l in x["friend"]["legs"]])
        any_seat = any(has_seat(l["seats"]) for l in seat_src)
        title = "12306 有票了！马上下单"
        if new_pairs:
            names = []
            for x in new_pairs:
                if x["comp"]["name"] not in names:
                    names.append(x["comp"]["name"])
            title += "（可与%s同行）" % "、".join(names[:2])
        title += "（有座）" if any_seat else "（仅无座）"

        # 极简推送走 GET，URL 有硬长度限制，单独给一份精简文本
        blocks = []
        for x in top_pairs:
            n = len(x["shared"])
            blocks.append("%s 与%s同行 %s｜你 %s %s到｜%s %s %s到" % (
                x["date"][5:], x["comp"]["name"], seg_short(x["shared"]),
                seg_short(x["mine"]["legs"][n:]) or "已到",
                x["mine"]["final"],
                x["comp"]["name"], seg_short(x["friend"]["legs"][n:]) or "已到",
                x["friend"]["final"]))
        for p in top_solo:
            blocks.append("%s %s %s %s到%s" % (
                p["date"][5:], seg_short(p["legs"]), p["seat_tag"],
                p["final"], sname(p["legs"][-1]["to_code"])))

        short = "【12306放票】" + blocks[0]
        for b in blocks[1:]:
            short += "\n备选：" + b
        if extra:
            short += "\n" + extra.strip("（）")
        short += "\n马上去12306下单！"

        ok, msg = push.send(title, body, short=short)
        log("微信推送: %s" % msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
