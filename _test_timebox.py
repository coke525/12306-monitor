# -*- coding: utf-8 -*-
"""冒烟测试：TimeBox 双向同步 + 日期候选。跑完自动关窗口，不联网。"""
import io
import os
import sys
import tkinter as tk
from tkinter import ttk

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
monitor_gui = __import__("monitor_gui")

root = tk.Tk()
root.withdraw()

TB = monitor_gui.TimeBox
fails = []


def ck(name, got, want):
    ok = got == want
    print("%-46s got=%-10r want=%-10r %s" % (name, got, want, "OK" if ok else "FAIL"))
    if not ok:
        fails.append(name)


v = tk.StringVar(value="")
tb = TB(root, v)
ck("初始空值 → var", v.get(), "")
ck("初始空值 → 小时框", tb.h.get(), TB.NONE)
ck("初始空值 → 分钟框", tb.m.get(), TB.NONE)

v.set("09:07")
ck("外部 set 09:07 → 小时", tb.h.get(), "09")
ck("外部 set 09:07 → 分钟", tb.m.get(), "07")

tb.h.set("23")
ck("选 23 点 → var", v.get(), "23:07")
tb.m.set("59")
ck("选 59 分 → var", v.get(), "23:59")

# 全天任意时刻都要能选
for hh in range(24):
    for mm in (0, 1, 30, 59):
        tb.h.set("%02d" % hh)
        tb.m.set("%02d" % mm)
        ck("全天可选 %02d:%02d" % (hh, mm), v.get(), "%02d:%02d" % (hh, mm))

# 只选小时 → 整点
tb.h.set("17")
tb.m.set(TB.NONE)
ck("只选小时 17 → 整点", v.get(), "17:00")
ck("只选小时 → 分钟框回写 00", tb.m.get(), "00")

# 只选分钟 → 不成立，应清空
v.set("")
tb.h.set(TB.NONE)
tb.m.set("45")
ck("只选分钟 → 清空", v.get(), "")
ck("只选分钟 → 分钟框被清", tb.m.get(), TB.NONE)

# 两个都选不限 → 不限
v.set("08:30")
tb.h.set(TB.NONE)
tb.m.set(TB.NONE)
ck("双不限 → var 清空", v.get(), "")

# 脏输入：控件不该炸，且要不认（显示不限），要不显示成标准 HH:MM
for bad, want in [("", "不限"), ("abc", "不限"), ("25:00", "不限"), ("24:01", "不限"),
                  ("9:5", "09:05"), ("07:1", "07:01"), ("7:1", "07:01"),
                  ("00:00", "00:00"), ("  18:20  ", "18:20"), ("18：20", "18:20")]:
    try:
        v.set(bad)
        got = "%s:%s" % (tb.h.get(), tb.m.get())
        if want == "不限":
            got = "不限" if tb.h.get() == TB.NONE and tb.m.get() == TB.NONE else got
        ck("控件容错 %r" % bad, got, want)
    except Exception as e:      # noqa: BLE001
        fails.append("异常 %r: %s" % (bad, e))
        print("异常 %r -> %s" % (bad, e))

# 落到配置里的时间：由核心统一归一化，保证字符串比较正确
tm = sys.modules.get("train_monitor") or __import__("train_monitor")
for bad, want in [("", None), (None, None), ("9:5", "09:05"), ("07:1", "07:01"),
                  ("  18:20  ", "18:20"), ("18：20", "18:20"),
                  ("25:00", None), ("abc", None), ("23:59", "23:59"),
                  ("0:0", "00:00")]:
    try:
        ck("核心归一化 %r" % bad, tm.norm_hm(bad), want)
    except Exception as e:      # noqa: BLE001
        fails.append("norm_hm 异常 %r: %s" % (bad, e))
        print("norm_hm 异常 %r -> %s" % (bad, e))

# 日期候选
ds = monitor_gui.date_choices()
print("\n日期候选 %d 个：%s … %s" % (len(ds), ds[0], ds[-1]))
ck("候选包含昨天(不在列表)", ds[0] == __import__("datetime").date.today().isoformat(), True)
if len(ds) < 30:
    fails.append("日期候选太少")

print("\n%s：%s" % ("全部通过" if not fails else "有失败", fails or ""))
root.destroy()
sys.exit(1 if fails else 0)
