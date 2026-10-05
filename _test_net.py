# -*- coding: utf-8 -*-
"""端到端：用假数据跑 build_plans + pair_companions，验证朋友终点=我的换乘站的情形"""
import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import train_monitor as tm  # noqa: E402

D = "2026-10-06"
SEATS = {"商务座": "-", "一等座": "-", "二等座": "8", "无座": "有"}


def row(start, arrive, n=8):
    return {"total": n, "start": start, "arrive": arrive, "seats": dict(SEATS)}


ROWS = {
    (D, "BFF", "ZZF", "K5346"): row("10:00", "13:00"),
    (D, "BFF", "ZAF", "K999"): row("11:00", "14:00"),
    (D, "ZZF", "JOF", "G1234"): row("14:00", "15:00"),
    (D, "ZZF", "JIF", "G5678"): row("14:30", "15:40"),
    (D, "JOF", "JIF", "C111"): row("15:30", "15:50"),
    (D, "ZZF", "LYF", "D222"): row("15:00", "16:20"),
    (D, "ZAF", "LYF", "D333"): row("15:10", "16:40"),
}

tm.STATION_NAME.update({"BFF": "宝丰", "ZZF": "郑州", "ZAF": "郑州东",
                        "JOF": "焦作", "JIF": "焦作西", "LYF": "洛阳"})
tm.DATES = [D]
tm.DATE_PREF = {D: {"dep_from": "00:00", "dep_to": "23:59",
                    "arr_zz_by": None, "final_by": "23:59"}}


def run(tag):
    tm.rebuild_legs()
    plans = tm.build_plans(ROWS, [D])
    mine = [p for p in plans if p["legs"][-1]["to_code"] == "JIF"]
    print("\n" + "=" * 76)
    print(tag)
    print("  换乘网络: %s" % " → ".join("/".join(x) for x in tm._layers()))
    print("  我自己到焦作西的方案 %d 个：" % len(mine))
    for p in mine:
        print("     - " + tm.plan_text(p).replace("[] ", ""))
    pairs = tm.pair_companions(plans)
    print("  同行组合 %d 个：" % len(pairs))
    for x in pairs:
        print("     " + tm.pair_text(x).replace("\n", "\n     "))
    return plans


# 场景 1：朋友的终点 = 郑州，而郑州正是我换乘去焦作西的地方（以前会把郑州从换乘层剔掉）
tm.ORIGINS, tm.MID1, tm.VIA, tm.VIA2, tm.DEST = ["BFF"], ["ZZF"], ["JOF"], [], "JIF"
tm.COMPANIONS = [{"name": "小明", "dest": "ZZF", "split_at": [], "tickets": 2,
                  "final_by": "", "max_transfers": None}]
p1 = run("场景1 朋友到郑州就下车（郑州同时是我换乘站）")
assert any(x["split_at"] == "ZZF" for x in tm.pair_companions(p1)), "场景1 应该能配对"

# 场景 2：朋友去洛阳（不是我的换乘站），正常配对
tm.COMPANIONS = [{"name": "小红", "dest": "LYF", "split_at": [], "tickets": 2,
                  "final_by": "", "max_transfers": None}]
tm.MID1 = ["ZZF", "ZAF"]
p2 = run("场景2 朋友去洛阳")
assert tm.pair_companions(p2), "场景2 应该能配对"

# 场景 3：没有同行人时行为不变
tm.COMPANIONS = []
p3 = run("场景3 无同行人（退化成单人模式）")
assert all(p["legs"][-1]["to_code"] == "JIF" for p in p3), "单人模式不应有别的终点"

print("\n全部场景通过。")
