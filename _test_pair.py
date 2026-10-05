# -*- coding: utf-8 -*-
"""离线验证同行配对：把"换乘次数不同 / 车上分手 / 有人直接到底 / 多人"全跑一遍"""
import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import train_monitor as tm  # noqa: E402

SEATS_OK = {"商务座": "-", "一等座": "-", "二等座": "9", "无座": "有"}
SEATS_1 = {"商务座": "-", "一等座": "-", "二等座": "1", "无座": "有"}


def leg(code, fr, to, fcode, tcode, start, arrive, seats=None):
    return {"code": code, "from": fr, "to": to, "from_code": fcode,
            "to_code": tcode, "start": start, "arrive": arrive,
            "seats": dict(seats or SEATS_OK)}


def plan(date, legs, hit=True):
    return {"date": date, "nlegs": len(legs), "final": legs[-1]["arrive"],
            "hit": hit, "buffers": [30] * max(0, len(legs) - 1),
            "needs": [25] * max(0, len(legs) - 1), "tight": False,
            "legs": legs, "seat_tag": "[全程有座]"}


def show(tag, pairs):
    print("\n" + "=" * 78)
    print(tag, "→ 组合数", len(pairs))
    for x in pairs:
        print("   " + tm.pair_text(x).replace("\n", "\n   "))
        print("   [need=%d ok=%s hit=%s kind=%s split=%s]"
              % (x["need"], x["tickets_ok"], x["hit"], x["kind"], x["split_at"]))
    return pairs


tm.DEST = "JIF"
tm.STATION_NAME.update({"ZZF": "郑州", "ZAF": "郑州东", "XXF": "新乡",
                        "LYF": "洛阳", "JOF": "焦作", "JIF": "焦作西",
                        "BFF": "宝丰", "PDF": "平顶山"})

# --- A 换乘次数不同：我换 1 次，朋友连换 3 次 -----------------------------
mine = plan("2026-10-06", [
    leg("K5346", "宝丰", "郑州", "BFF", "ZZF", "10:20", "13:41"),
    leg("G1234", "郑州", "焦作西", "ZZF", "JIF", "14:20", "15:20")])
fri = plan("2026-10-06", [
    leg("K5346", "宝丰", "郑州", "BFF", "ZZF", "10:20", "13:41"),
    leg("D1", "郑州", "新乡", "ZZF", "XXF", "14:00", "14:40"),
    leg("D2", "新乡", "洛阳", "XXF", "LYF", "15:00", "16:30"),
    leg("D3", "洛阳", "三门峡", "LYF", "SMF", "17:00", "18:20")])
tm.COMPANIONS = [{"name": "小明", "dest": "SMF", "split_at": [], "tickets": 2,
                  "final_by": "", "max_transfers": None}]
show("A 我换1次/他换3次", tm.pair_companions([mine, fri]))

# --- B 同站分手，之后各换各的 ------------------------------------------
fri = plan("2026-10-06", [
    leg("K5346", "宝丰", "郑州", "BFF", "ZZF", "10:20", "13:41"),
    leg("D8", "郑州", "洛阳", "ZZF", "LYF", "14:10", "15:30")])
tm.COMPANIONS = [{"name": "小明", "dest": "LYF", "split_at": [], "tickets": 2,
                  "final_by": "", "max_transfers": None}]
show("B 郑州同站分手各走各的", tm.pair_companions([mine, fri]))

# --- C 车上分手：我在郑州下，他同车继续坐到新乡才下 ----------------------
mine2 = plan("2026-10-06", [
    leg("K5346", "宝丰", "郑州", "BFF", "ZZF", "10:20", "13:41"),
    leg("G1234", "郑州", "焦作西", "ZZF", "JIF", "14:20", "15:20")])
fri2 = plan("2026-10-06", [
    leg("K5346", "宝丰", "新乡", "BFF", "XXF", "10:20", "14:30"),
    leg("D9", "新乡", "洛阳", "XXF", "LYF", "15:00", "16:30")])
tm.COMPANIONS = [{"name": "小明", "dest": "LYF", "split_at": [], "tickets": 2,
                  "final_by": "", "max_transfers": None}]
show("C 我先下车/他同车坐到新乡", tm.pair_companions([mine2, fri2]))

# --- D 车上分手：他先下（郑州），我同车继续坐到新乡再转焦作西 ------------
mine3 = plan("2026-10-06", [
    leg("K5346", "宝丰", "新乡", "BFF", "XXF", "10:20", "14:30"),
    leg("G88", "新乡", "焦作西", "XXF", "JIF", "15:10", "16:10")])
fri3 = plan("2026-10-06", [
    leg("K5346", "宝丰", "郑州", "BFF", "ZZF", "10:20", "13:41"),
    leg("D9", "郑州", "洛阳", "ZZF", "LYF", "14:10", "15:30")])
tm.COMPANIONS = [{"name": "小明", "dest": "LYF", "split_at": [], "tickets": 2,
                  "final_by": "", "max_transfers": None}]
show("D 他先下车/我同车继续", tm.pair_companions([mine3, fri3]))

# --- E 朋友直接到底：分手站就是他的终点 ---------------------------------
fri4 = plan("2026-10-06", [
    leg("K5346", "宝丰", "郑州", "BFF", "ZZF", "10:20", "13:41")])
tm.COMPANIONS = [{"name": "小明", "dest": "ZZF", "split_at": [], "tickets": 2,
                  "final_by": "", "max_transfers": None}]
show("E 他到郑州就到底了", tm.pair_companions([mine, fri4]))

# --- F 我直达焦作西（1段），朋友换 2 次去洛阳 ----------------------------
mine_direct = plan("2026-10-06", [
    leg("K258", "宝丰", "焦作西", "BFF", "JIF", "11:00", "16:00")])
fri5 = plan("2026-10-06", [
    leg("K258", "宝丰", "郑州", "BFF", "ZZF", "11:00", "14:10"),
    leg("D9", "郑州", "洛阳", "ZZF", "LYF", "15:00", "16:20")])
tm.COMPANIONS = [{"name": "小明", "dest": "LYF", "split_at": [], "tickets": 2,
                  "final_by": "", "max_transfers": None}]
show("F 我直达/他换乘，同一趟车中途分手", tm.pair_companions([mine_direct, fri5]))

# --- G 同车多段：前两段都一样，在焦作站分手 ------------------------------
mine6 = plan("2026-10-06", [
    leg("K5346", "宝丰", "郑州", "BFF", "ZZF", "10:20", "13:41"),
    leg("G1234", "郑州", "焦作", "ZZF", "JOF", "14:20", "15:00"),
    leg("C1", "焦作", "焦作西", "JOF", "JIF", "15:30", "15:50")])
fri6 = plan("2026-10-06", [
    leg("K5346", "宝丰", "郑州", "BFF", "ZZF", "10:20", "13:41"),
    leg("G1234", "郑州", "焦作", "ZZF", "JOF", "14:20", "15:00"),
    leg("C2", "焦作", "洛阳", "JOF", "LYF", "15:40", "17:20")])
tm.COMPANIONS = [{"name": "小明", "dest": "LYF", "split_at": [], "tickets": 2,
                  "final_by": "", "max_transfers": None}]
show("G 同行两段后在焦作分手", tm.pair_companions([mine6, fri6]))

# --- H 多人：小明去洛阳、小红去新乡，三个人一起坐到郑州 ------------------
fri_xh = plan("2026-10-06", [
    leg("K5346", "宝丰", "郑州", "BFF", "ZZF", "10:20", "13:41"),
    leg("D8", "郑州", "新乡", "ZZF", "XXF", "14:10", "15:00")])
tm.COMPANIONS = [
    {"name": "小明", "dest": "LYF", "split_at": [], "tickets": 2,
     "final_by": "", "max_transfers": None},
    {"name": "小红", "dest": "XXF", "split_at": [], "tickets": 3,
     "final_by": "", "max_transfers": None}]
show("H 同时带两个人（need=1+1+2=4）", tm.pair_companions([mine, fri, fri_xh]))

# --- I 限定分手站 + 换乘上限 + 起点不同 + 余票不足 -----------------------
tm.COMPANIONS = [{"name": "限定ZZF", "dest": "LYF", "split_at": ["XXF"],
                  "tickets": 2, "final_by": "", "max_transfers": None}]
r = show("I1 只允许在新乡分手（本例只能郑州分手）→ 应为 0",
         tm.pair_companions([mine, fri]))

tm.COMPANIONS = [{"name": "必须直达", "dest": "LYF", "split_at": [],
                  "tickets": 2, "final_by": "", "max_transfers": 0}]
show("I2 要求他直达（他是换乘方案）→ 应为 0", tm.pair_companions([mine, fri]))

tm.COMPANIONS = [{"name": "允许1次换乘", "dest": "LYF", "split_at": [],
                  "tickets": 2, "final_by": "", "max_transfers": 1}]
show("I3 他最多换 1 次 → 应有结果", tm.pair_companions([mine, fri]))

other_start = plan("2026-10-06", [
    leg("K5346", "平顶山", "郑州", "PDF", "ZZF", "09:50", "13:41"),
    leg("D8", "郑州", "洛阳", "ZZF", "LYF", "14:10", "15:30")])
tm.COMPANIONS = [{"name": "异地出发", "dest": "LYF", "split_at": [],
                  "tickets": 2, "final_by": "", "max_transfers": None}]
show("I4 他从别的站上车（目前不支持）→ 应为 0", tm.pair_companions([mine, other_start]))

poor = plan("2026-10-06", [
    leg("K5346", "宝丰", "郑州", "BFF", "ZZF", "10:20", "13:41", SEATS_1),
    leg("D8", "郑州", "洛阳", "ZZF", "LYF", "14:10", "15:30")])
poor_mine = plan("2026-10-06", [
    leg("K5346", "宝丰", "郑州", "BFF", "ZZF", "10:20", "13:41", SEATS_1),
    leg("G1234", "郑州", "焦作西", "ZZF", "JIF", "14:20", "15:20")])
tm.COMPANIONS = [{"name": "票不够", "dest": "LYF", "split_at": [],
                  "tickets": 2, "final_by": "", "max_transfers": None}]
show("I5 共同段只剩 1 张 → tickets_ok=False", tm.pair_companions([poor_mine, poor]))

print("\n全部用例跑完。")
