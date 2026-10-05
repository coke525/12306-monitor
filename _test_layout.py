# -*- coding: utf-8 -*-
"""界面几何校验：确认表头文字和它下面的输入框左边严格对齐（含新加的时间控件）。"""
import io
import os
import sys
import tkinter as tk

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
mg = __import__("monitor_gui")

root = tk.Tk()
root.withdraw()
_out = sys.stdout          # App 会把 print 重定向到界面里的文本框，先留一份
app = mg.App(root)
sys.stdout = _out
sys.stderr = _out          # 别让报错也跑进界面里看不见
root.update_idletasks()

fails = []


def check(body, name, cols):
    head = [w for w in body.grid_slaves(row=0)]
    head.sort(key=lambda w: w.winfo_rootx())
    print("\n== %s：表头 %d 列" % (name, len(head)))
    for col in range(len(head)):
        xs = []
        for r in range(1, 99):
            cells = body.grid_slaves(row=r, column=col)
            if not cells:
                if r > 1:
                    break
                continue
            xs.append(round(cells[0].winfo_rootx() - head[col].winfo_rootx(), 1))
        got = "对齐" if xs and max(abs(x) for x in xs) <= 1 else xs
        print("  第%d列 %-16s 偏移 %s  %s" % (
            col, cols[col] if col < len(cols) else "?", xs,
            "OK" if (xs and max(abs(x) for x in xs) <= 1) else "FAIL"))
        if not (xs and max(abs(x) for x in xs) <= 1):
            fails.append("%s 第%d列" % (name, col))


check(app.dbody, "日期与时间窗口",
      ["日期", "最早出发", "最晚出发", "最晚到换乘站", "最晚到终点"])
for w in app.dbody.grid_slaves(row=1, column=1):
    print("  第1行「最早出发」是 %s（宽 %d px）" % (
        type(w).__name__, w.winfo_width()))
for w in app.dbody.grid_slaves(row=1, column=0):
    # 日期是「年/月/日」三个下拉组成的复合控件，不再是一整个 Combobox
    if isinstance(w, mg.DateBox):
        print("  第1行「日期」是 DateBox（年 %d 项 / 月 %d 项 / 日 %d 项，宽 %d px）" % (
            len(w.cby["values"]), len(w.cbm["values"]), len(w.cbd["values"]),
            w.winfo_width()))
    else:
        print("  第1行「日期」是 %s（宽 %d px）" % (type(w).__name__, w.winfo_width()))

# 同行人区（只有加了朋友才有数据行）
app.add_comp({"name": "小明", "dest": "ZZF", "split_at": [],
              "tickets": 2, "final_by": "18:07", "max_transfers": None})
root.update_idletasks()
check(app.cbody, "同行人",
      ["称呼", "他的终点", "分手站", "票数", "他最晚到达", "他最多换乘"])
last = app.crows[-1]
print("  同行人「他最晚到达」读回 = %r（应为 18:07）" % last[4].get())

root.destroy()
print("\n%s：%s" % ("对齐全部通过" if not fails else "有错位的列", fails or ""))
sys.exit(1 if fails else 0)
