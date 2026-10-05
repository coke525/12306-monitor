# -*- coding: utf-8 -*-
"""冒烟测试：DateBox 年月日不限制。跑完自动关窗口，不联网。"""
import io
import os
import sys
import tkinter as tk

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
monitor_gui = __import__("monitor_gui")

root = tk.Tk()
root.withdraw()

DB = monitor_gui.DateBox
fails = []


def ck(name, got, want):
    ok = got == want
    print("%-46s got=%-12r want=%-12r %s" % (name, got, want, "OK" if ok else "FAIL"))
    if not ok:
        fails.append(name)


v = tk.StringVar(value="")
db = DB(root, v)
ck("初始空值 → var", v.get(), "")
ck("初始空值 → 年框", db.y.get(), "")

v.set("2026-10-07")
ck("外部 set → 年", db.y.get(), "2026")
ck("外部 set → 月", db.mo.get(), "10")
ck("外部 set → 日", db.d.get(), "07")

# 年份不限：下拉只是常用值，手输任意年份都认
ck("年份框可手输（非 readonly）", str(db.cby["state"]), "normal")
for yy in (1999, 2026, 2030, 2099):
    db.y.set(str(yy))
    ck("任意年份 %d" % yy, v.get(), "%d-10-07" % yy)

# 月份不限：1-12 全给
ck("月份候选 12 个", len(db.cbm["values"]), 12)
for mm in range(1, 13):
    db.mo.set("%02d" % mm)
    ck("任意月份 %02d" % mm, v.get(), "2099-%02d-07" % mm)

# 日号跟着年月走：大小月 / 闰年
db.y.set("2026")
db.mo.set("01")
db.d.set("31")
ck("1月31日", v.get(), "2026-01-31")
db.mo.set("02")
ck("切到2月 → 日号压到28", v.get(), "2026-02-28")
ck("2月候选只有28天", len(db.cbd["values"]), 28)
db.y.set("2028")            # 闰年
ck("闰年2月 → 29天", len(db.cbd["values"]), 29)
db.d.set("29")
ck("闰年2月29日", v.get(), "2028-02-29")
db.y.set("2027")
ck("平年切走 → 压回28", v.get(), "2027-02-28")
db.mo.set("04")
ck("4月只有30天", len(db.cbd["values"]), 30)
db.mo.set("12")
ck("12月有31天", len(db.cbd["values"]), 31)

# 脏输入不该炸
for bad in ["", "abc", "2026/10/07", "26-10-7", "2026-13-01", "2026-00-10"]:
    try:
        v.set(bad)
        print("  容错 %-12r -> var=%r (y=%r m=%r d=%r)"
              % (bad, v.get(), db.y.get(), db.mo.get(), db.d.get()))
    except Exception as e:      # noqa: BLE001
        fails.append("异常 %r: %s" % (bad, e))
        print("  异常 %r -> %s" % (bad, e))

# 没选全 = 空
v.set("2026-10-07")
db.mo.set("")
ck("月份清空 → var 空", v.get(), "")
db.mo.set("10")
db.d.set("07")
ck("重新选全 → 拼回", v.get(), "2026-10-07")

print("\n%s：%s" % ("全部通过" if not fails else "有失败", fails or ""))
root.destroy()
sys.exit(1 if fails else 0)
