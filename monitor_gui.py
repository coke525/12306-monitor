# -*- coding: utf-8 -*-
"""
12306 余票监控 · 图形配置工具
=============================
双击 exe 打开，界面里直接填行程偏好，不用碰任何代码和 JSON。

    [保存配置]        写到 exe 旁边的 monitor_config.json
    [立即扫描]        本地跑一轮，结果直接显示在下方（不推送）
    [测试微信推送]     发一条测试消息，确认链路通
    [同步到 GitHub]    把配置推到云端仓库，定时任务下一轮自动生效（关机也跑）
"""

import base64
import io
import json
import os
import ssl
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request

import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

BASE = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
        else os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import train_monitor as tm        # noqa: E402
import push as _push              # noqa: E402

CONFIG_FILE = os.path.join(BASE, "monitor_config.json")
CRED_FILE = os.path.join(BASE, "push_config.json")

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


# ---------------------------------------------------------------- IO 辅助

def load_cfg():
    if os.path.exists(CONFIG_FILE):
        try:
            return json.load(io.open(CONFIG_FILE, encoding="utf-8")) or {}
        except Exception:
            pass
    return {}


def save_cfg(cfg):
    with io.open(CONFIG_FILE, "w", encoding="utf-8") as fp:
        json.dump(cfg, fp, ensure_ascii=False, indent=2)


def load_cred():
    if os.path.exists(CRED_FILE):
        try:
            return json.load(io.open(CRED_FILE, encoding="utf-8")) or {}
        except Exception:
            pass
    return {}


def save_cred(c):
    with io.open(CRED_FILE, "w", encoding="utf-8") as fp:
        json.dump(c, fp, ensure_ascii=False, indent=2)


def split_codes(s):
    return [x.strip().upper() for x in (s or "").replace("，", ",").split(",") if x.strip()]


def station_map():
    """内置站名表 + monitor_config.json 里的全量电报码（3404 个站）"""
    st = dict(getattr(tm, "STATION_NAME", {}) or {})
    st.update(load_cfg().get("stations") or {})
    return st


def gh(method, path, token, payload=None):
    """GitHub REST 调用"""
    d = None
    h = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
         "User-Agent": "12306-monitor-gui"}
    if payload is not None:
        d = json.dumps(payload).encode("utf-8")
        h["Content-Type"] = "application/json"
    req = urllib.request.Request("https://api.github.com" + path, data=d,
                                 method=method, headers=h)
    try:
        b = urllib.request.urlopen(req, timeout=30, context=CTX).read()
        return (json.loads(b.decode("utf-8", "ignore")) if b.strip() else {}), None
    except urllib.error.HTTPError as e:
        return None, "HTTP %d %s" % (e.code, e.read().decode("utf-8", "ignore")[:120])
    except Exception as e:
        return None, repr(e)[:150]


class TextOut:
    """把 print / log 接到界面下方的文本框"""

    def __init__(self, widget):
        self.w = widget

    def write(self, s):
        if not s or s.strip() == "":
            return
        def put():
            self.w.insert("end", s)
            self.w.see("end")
        self.w.after(0, put)

    def flush(self):
        pass


# ---------------------------------------------------------------- 主界面

class App:

    def __init__(self, root):
        self.root = root
        root.title("12306 余票监控 · 配置工具")
        root.geometry("940x760")

        cfg = load_cfg()
        cred = load_cred()

        # ---------- 行程 ----------
        box = ttk.LabelFrame(root, text=" 行程（填电报码，多个用逗号隔开）")
        box.pack(fill="x", padx=10, pady=(10, 6))
        self.v_origins = tk.StringVar(value=",".join(cfg.get("origins") or ["PDF", "BFF", "PEN"]))
        self.v_dest = tk.StringVar(value=cfg.get("dest") or "JIF")
        self.v_mid = tk.StringVar(value=",".join(cfg.get("transfer_cities") or ["ZZF", "ZAF", "ZIF"]))
        self.v_via = tk.StringVar(value=cfg.get("via") or "JOF")
        self.v_fastmid = tk.StringVar(value=",".join(cfg.get("fast_mid") or ["ZZF", "ZAF"]))
        for i, (label, var, tip) in enumerate([
            ("出发站", self.v_origins, "可接受的所有出发站"),
            ("终点", self.v_dest, "最终目的地"),
            ("换乘城市", self.v_mid, "第一次换乘允许的车站"),
            ("二次换乘", self.v_via, "没有就填成和终点一样"),
            ("主干换乘站", self.v_fastmid, "fast 模式只扫这几个，通常取换乘城市前两个"),
        ]):
            ttk.Label(box, text=label).grid(row=0, column=i * 2, padx=(10, 3), pady=8, sticky="e")
            e = ttk.Entry(box, textvariable=var, width=14)
            e.grid(row=0, column=i * 2 + 1, padx=(0, 6), sticky="w")
            e.bind("<Enter>", lambda ev, t=tip: self.hint(t))
        ttk.Label(box, text="路线 = 出发站 → 换乘城市 → 二次换乘 → 终点（1~3 段）",
                  foreground="#888").grid(row=1, column=0, columnspan=10, sticky="w", padx=10, pady=(0, 6))

        # ---------- 日期与时段 ----------
        dbox = ttk.LabelFrame(root, text=" 日期与时间窗口（每天一行；留空 = 不限制）")
        dbox.pack(fill="x", padx=10, pady=6)
        head = ["日期", "最早出发", "最晚出发", "最晚到换乘站", "最晚到终点"]
        for i, h in enumerate(head):
            ttk.Label(dbox, text=h, foreground="#555").grid(row=0, column=i, padx=6, pady=(6, 2))
        self.rows = []
        self.dbody = ttk.Frame(dbox)
        self.dbody.grid(row=1, column=0, columnspan=5, sticky="w")
        prefs = cfg.get("date_prefs") or {}
        for d in (cfg.get("dates") or []):
            self.add_row(d, prefs.get(d) or {})
        if not self.rows:
            self.add_row("", {})
        btns = ttk.Frame(dbox)
        btns.grid(row=2, column=0, columnspan=5, sticky="w", padx=6, pady=6)
        ttk.Button(btns, text="+ 加一天", command=lambda: self.add_row("", {})).pack(side="left")
        ttk.Button(btns, text="− 删最后一行", command=self.del_row).pack(side="left", padx=6)
        ttk.Label(btns, text="日期必须和上面窗口成对出现；只填日期不加窗口 → 那天不会推送",
                  foreground="#a00").pack(side="left", padx=12)

        # ---------- 查车站代码 ----------
        qbox = ttk.LabelFrame(root, text=" 查车站代码（输中文站名或三字母代码，全国车站都能查）")
        qbox.pack(fill="x", padx=10, pady=6)
        self.stations = station_map()
        self.v_q = tk.StringVar()
        self.q_res = tk.StringVar(value="共 %d 个车站已内置" % len(self.stations))
        e = ttk.Entry(qbox, textvariable=self.v_q, width=18)
        e.pack(side="left", padx=(10, 4), pady=8)
        e.bind("<Return>", lambda ev: self.on_query())
        ttk.Button(qbox, text="查", command=self.on_query, width=4).pack(side="left")
        ttk.Label(qbox, textvariable=self.q_res, foreground="#06c").pack(side="left", padx=10)

        # ---------- 选项 + 凭据 ----------
        obox = ttk.Frame(root)
        obox.pack(fill="x", padx=10, pady=6)
        left = ttk.LabelFrame(obox, text=" 扫描选项")
        left.pack(side="left", fill="x", expand=True)
        self.v_mode = tk.StringVar(value=cfg.get("scan_mode") or "fast")
        self.v_seated = tk.BooleanVar(value=bool(cfg.get("only_seated", False)))
        ttk.Label(left, text="模式").grid(row=0, column=0, padx=(10, 3), pady=8)
        ttk.Combobox(left, textvariable=self.v_mode, values=["fast", "full"],
                     width=8, state="readonly").grid(row=0, column=1, sticky="w")
        ttk.Checkbutton(left, text="只推全程有座（勾上则无座方案不提醒）",
                        variable=self.v_seated).grid(row=0, column=2, padx=14)
        ttk.Label(left, text="fast≈40秒/轮　full≈90秒/轮",
                  foreground="#888").grid(row=0, column=3)

        right = ttk.LabelFrame(obox, text=" 推送与同步凭据（只存在本机）")
        right.pack(side="left", fill="x", expand=True, padx=(10, 0))
        self.v_spt = tk.StringVar(value=cred.get("wxpusher_spt") or "")
        self.v_pat = tk.StringVar(value=cred.get("github_pat") or "")
        self.v_repo = tk.StringVar(value=cred.get("github_repo") or "coke525/12306-monitor")
        for i, (lb, var, show) in enumerate([
            ("微信 SPT", self.v_spt, ""),
            ("GitHub PAT", self.v_pat, "*"),
            ("仓库", self.v_repo, ""),
        ]):
            ttk.Label(right, text=lb).grid(row=i, column=0, padx=(10, 3), pady=4, sticky="e")
            ttk.Entry(right, textvariable=var, width=30, show=show).grid(row=i, column=1, sticky="w")

        # ---------- 按钮 ----------
        bbox = ttk.Frame(root)
        bbox.pack(fill="x", padx=10, pady=8)
        self.busy = False
        for text, cmd in [("保存配置", self.on_save),
                          ("立即扫描（不推送）", self.on_scan),
                          ("测试微信推送", self.on_test),
                          ("同步到 GitHub（云端生效）", self.on_sync)]:
            ttk.Button(bbox, text=text, command=cmd).pack(side="left", padx=4)

        # ---------- 输出 ----------
        self.hint_var = tk.StringVar(value="鼠标移到输入框上看说明")
        ttk.Label(root, textvariable=self.hint_var, foreground="#06c").pack(anchor="w", padx=12)
        self.out = scrolledtext.ScrolledText(root, height=18, font=("Consolas", 10))
        self.out.pack(fill="both", expand=True, padx=10, pady=(2, 10))
        sys.stdout = TextOut(self.out)
        sys.stderr = TextOut(self.out)
        self.say("就绪。改完点「保存配置」，要云端生效再点「同步到 GitHub」。\n")
        self.say("常用站：" + "  ".join(
            "%s=%s" % (k, self.stations[k]) for k in
            ["PEN", "PDF", "BFF", "NFF", "ZZF", "ZAF", "ZIF", "JOF", "JIF", "NYF"]
            if k in self.stations) + "\n")
        self.say("想查别的站，用上面的「查车站代码」框，输中文名即可。\n\n")

    def on_query(self):
        kw = self.v_q.get().strip()
        if not kw:
            return
        hits = [(k, v) for k, v in self.stations.items() if kw in v]
        if not hits and len(kw) <= 4:          # 也支持反过来：输代码查站名
            hits = [(k, v) for k, v in self.stations.items() if kw.upper() in k]
        hits.sort(key=lambda x: len(x[1]))
        if not hits:
            self.q_res.set("没找到「%s」，换个说法试试（如「洛阳」不要带「站」）" % kw)
        else:
            self.q_res.set("  ".join("%s=%s" % (k, v) for k, v in hits[:10])
                           + ("  …共 %d 个" % len(hits) if len(hits) > 10 else ""))

    # ---- 表格 ----
    def add_row(self, date, p):
        r = len(self.rows)
        vs = [tk.StringVar(value=date)]
        for k in ("dep_from", "dep_to", "arr_zz_by", "final_by"):
            vs.append(tk.StringVar(value=(p.get(k) or "")))
        for c, v in enumerate(vs):
            ttk.Entry(self.dbody, textvariable=v, width=13).grid(row=r, column=c, padx=6, pady=2)
        self.rows.append(vs)

    def del_row(self):
        if len(self.rows) <= 1:
            return
        vs = self.rows.pop()
        for v in vs:
            pass
        for w in self.dbody.grid_slaves(row=len(self.rows)):
            w.destroy()

    def hint(self, t):
        self.hint_var.set(t)

    # ---- 输出 ----
    def say(self, s):
        self.out.insert("end", s)
        self.out.see("end")

    def collect(self):
        """把界面内容收成配置字典，保留原有 _ 开头的说明字段"""
        cfg = load_cfg()
        dates, prefs = [], {}
        for vs in self.rows:
            d = vs[0].get().strip()
            if not d:
                continue
            dates.append(d)
            prefs[d] = {"dep_from": vs[1].get().strip() or "00:00",
                        "dep_to": vs[2].get().strip() or "23:59",
                        "arr_zz_by": vs[3].get().strip() or None,
                        "final_by": vs[4].get().strip() or None}
        if not dates:
            raise ValueError("至少填一个日期")
        cfg["dates"] = dates
        cfg["date_prefs"] = prefs
        cfg["origins"] = split_codes(self.v_origins.get())
        cfg["dest"] = self.v_dest.get().strip().upper()
        cfg["transfer_cities"] = split_codes(self.v_mid.get())
        cfg["via"] = self.v_via.get().strip().upper()
        cfg["fast_mid"] = split_codes(self.v_fastmid.get())
        cfg["scan_mode"] = self.v_mode.get()
        cfg["only_seated"] = self.v_seated.get()
        return cfg

    # ---- 动作 ----
    def on_save(self):
        try:
            cfg = self.collect()
        except ValueError as e:
            messagebox.showerror("填错了", str(e))
            return
        save_cfg(cfg)
        c = load_cred()
        c["wxpusher_spt"] = self.v_spt.get().strip()
        c["github_pat"] = self.v_pat.get().strip()
        c["github_repo"] = self.v_repo.get().strip()
        save_cred(c)
        self.say("[保存] 已写入 %s\n" % CONFIG_FILE)

    def on_scan(self):
        if self.busy:
            return
        self.on_save()
        self.busy = True
        threading.Thread(target=self._scan, daemon=True).start()

    def _scan(self):
        try:
            tm.load_config()
            dates, routes = tm.DATES, tm.LEG1 + tm.LEG2 + tm.LEG3 + tm.EXTRA_WATCH
            print("扫描中：%d 个日期 × %d 条路线，约 40-90 秒 …\n" % (len(dates), len(routes)))
            rows = tm.scan(dates, routes)
            avail = {k: v for k, v in rows.items() if v["total"] > 0}
            plans = tm.build_plans(rows, dates)
            hits = [p for p in plans if p["hit"]]
            if tm.ONLY_SEATED:
                hits = [p for p in hits if all(tm.has_seat(l["seats"]) for l in p["legs"])]
            hits.sort(key=lambda p: p["final"])
            print("扫到 %d 趟车，有票 %d 趟，可行方案 %d 个，命中你的窗口 %d 个\n"
                  % (len(rows), len(avail), len(plans), len(hits)))
            if not hits:
                print("当前没有符合窗口的方案。云端会继续盯，一有票就推微信。\n")
            for p in hits[:20]:
                print("  ★ " + tm.plan_text(p) + "\n")
            if len(hits) > 20:
                print("  …另有 %d 个，已省略\n" % (len(hits) - 20))
        except Exception as e:
            import traceback
            print("扫描失败：%s\n%s\n" % (e, traceback.format_exc()))
        finally:
            self.busy = False
            print("扫描结束。\n")

    def on_test(self):
        if not self.v_spt.get().strip():
            messagebox.showerror("缺少 token", "先填微信 SPT（SPT_ 开头那串）")
            return
        self.on_save()
        ok, msg = _push.send("12306 监控测试",
                             "<p>配置工具连通性测试。收到这条说明微信推送正常。</p>",
                             short="【12306】配置工具测试，收到即正常")
        self.say("[推送] %s  %s\n" % ("成功，去看微信" if ok else "失败", msg or ""))
        messagebox.showinfo("结果", "推送成功，检查微信" if ok else "推送失败：%s" % (msg or ""))

    def on_sync(self):
        pat = self.v_pat.get().strip()
        repo = self.v_repo.get().strip()
        if not pat or "/" not in repo:
            messagebox.showerror("缺配置",
                                 "需要 GitHub PAT（ghp_ 开头）和仓库名（用户名/仓库名）\n\n"
                                 "PAT 获取：github.com/settings/tokens → Generate new token (classic)\n"
                                 "勾选 repo 即可。同步完可以立刻去删掉，不影响已建好的定时任务。")
            return
        self.on_save()
        self.busy = True
        threading.Thread(target=self._sync, args=(pat, repo), daemon=True).start()

    def _sync(self, pat, repo):
        try:
            path = "/repos/%s/contents/monitor_config.json" % repo
            old, err = gh("GET", path + "?ref=main", pat)
            if err:
                print("[同步] 读取远端失败：%s\n" % err)
                return
            content = io.open(CONFIG_FILE, "rb").read()
            payload = {"message": "config: 通过配置工具更新",
                       "content": base64.b64encode(content).decode("ascii"),
                       "branch": "main"}
            if isinstance(old, dict) and old.get("sha"):
                payload["sha"] = old["sha"]
            new, err2 = gh("PUT", path, pat, payload)
            print("[同步] %s\n" % ("已推送到云端，下一轮扫描自动生效"
                                   if not err2 else "失败：" + err2))
        finally:
            self.busy = False


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
