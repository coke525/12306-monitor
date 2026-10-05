# -*- coding: utf-8 -*-
"""
12306 余票监控 · 图形配置工具
=============================
双击 exe 打开，界面里直接填行程偏好，不用碰任何代码和 JSON。

    [保存配置]        写到 exe 旁边的 monitor_config.json
    [立即扫描]        本地跑一轮，结果直接显示在下方（不推送）
    [测试微信推送]     发一条测试消息，确认链路通
    [同步到 GitHub]    把配置推到云端仓库，定时任务下一轮自动生效（关机也跑）
    [关闭云端监控]     停用云端定时任务，不再扫描、不再推微信（配置全保留）
    [恢复云端监控]     把定时任务装回仓库，继续监控
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

# 云端定时任务的原始内容，内嵌在 exe 里。
# 这样「恢复监控」不依赖本地有没有 .github/workflows/ticket.yml，
# exe 被拷到任何目录都能把定时任务原样装回仓库。
WF_PATH = ".github/workflows/ticket.yml"
WF_TEMPLATE = """name: 12306 余票监控

on:
  schedule:
    # cron 用 UTC，北京时间 = UTC+8。下面两条覆盖 北京 07:00-23:55
    - cron: "*/5 23 * * *"      # 北京 07:00-07:55
    - cron: "*/5 0-15 * * *"    # 北京 08:00-23:55
    # 备用触发点（整点/半点），防止 */5 被 GitHub 丢调度
    - cron: "3,33 0-15 * * *"
    - cron: "7,37 23 * * *"
  workflow_dispatch:            # 手动点一下立刻跑

# 同一时刻只允许一个监控任务，避免重复推送和提交冲突
concurrency:
  group: ticket-monitor
  cancel-in-progress: false

jobs:
  monitor:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    permissions:
      contents: write           # 需要把状态快照提交回仓库
    steps:
      - name: 拉取代码
        uses: actions/checkout@v4

      - name: 准备 Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      # GitHub 的定时调度经常丢，所以不依赖它：单次任务内部连续扫描 27 分钟，
      # 每轮约 62 秒 + 间隔 15 秒 —— 只要一天触发几次就能近乎连续覆盖
      - name: 连续扫描（最多 27 分钟）
        continue-on-error: true
        env:
          WXPUSHER_SPT: ${{ secrets.WXPUSHER_SPT }}
          PUSHPLUS_TOKEN: ${{ secrets.PUSHPLUS_TOKEN }}
          WXPUSHER_TOKEN: ${{ secrets.WXPUSHER_TOKEN }}
          WXPUSHER_UID: ${{ secrets.WXPUSHER_UID }}
          STATE_FILE: cloud_state.json
          SCAN_SECONDS: "1620"
        run: |
          START=$(date +%s)
          i=0
          while true; do
            NOW=$(date +%s)
            ELAPSED=$((NOW - START))
            if [ "$ELAPSED" -ge "$SCAN_SECONDS" ]; then
              echo "已跑满 ${SCAN_SECONDS}s，结束（共 $i 轮）"
              break
            fi
            i=$((i + 1))
            echo "=== 第 $i 轮  已用 ${ELAPSED}s  $(date -u +'%H:%M:%S') UTC ==="
            python cloud_monitor.py
            sleep 15
          done

      - name: 回写状态快照
        run: |
          git config user.name "ticket-bot"
          git config user.email "ticket-bot@users.noreply.github.com"
          git add -f cloud_state.json
          if git diff --cached --quiet; then
            echo "状态无变化，不提交"
          else
            git commit -m "state: $(date -u +'%Y-%m-%dT%H:%M:%SZ')"
            git pull --rebase origin "${GITHUB_REF_NAME}" || true
            git push || echo "push 冲突/失败，下次运行会覆盖，忽略"
          fi
"""


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
        def joins(v):
            """配置里可能是字符串也可能是列表，统一拼成逗号串填进输入框"""
            if isinstance(v, (list, tuple)):
                return ",".join(str(x) for x in v)
            return str(v or "")

        self.v_origins = tk.StringVar(value=joins(cfg.get("origins") or ["PDF", "BFF", "PEN"]))
        self.v_dest = tk.StringVar(value=cfg.get("dest") or "JIF")
        self.v_mid = tk.StringVar(value=joins(cfg.get("transfer_cities") or ["ZZF", "ZAF", "ZIF"]))
        self.v_via = tk.StringVar(value=joins(cfg.get("via") or "JOF"))
        self.v_via2 = tk.StringVar(value=joins(cfg.get("via2")))
        self.v_city = tk.StringVar(value=joins(cfg.get("city_stations") or cfg.get("via") or "JOF"))
        self.v_fastmid = tk.StringVar(value=joins(cfg.get("fast_mid") or ["ZZF", "ZAF"]))
        self.v_mt = tk.StringVar(value=str(cfg.get("max_transfers") or 3))

        # 第一行：主干行程
        row1 = [
            ("出发站", self.v_origins, "可接受的所有出发站，多个用逗号隔开"),
            ("终点", self.v_dest, "最终目的地"),
            ("换乘城市", self.v_mid, "第一次换乘允许的车站（层1）"),
            ("主干换乘站", self.v_fastmid, "fast 模式只扫这几个，通常取换乘城市前两个"),
        ]
        for i, (label, var, tip) in enumerate(row1):
            ttk.Label(box, text=label).grid(row=0, column=i * 2, padx=(10, 3), pady=(8, 2), sticky="e")
            e = ttk.Entry(box, textvariable=var, width=14)
            e.grid(row=0, column=i * 2 + 1, padx=(0, 6), sticky="w")
            e.bind("<Enter>", lambda ev, t=tip: self.hint(t))

        # 第二行：多换乘配置
        row2 = [
            ("二次换乘", self.v_via, "第二次换乘站（层2），可填多个：JOF,XXF。不想要就留空"),
            ("三次换乘", self.v_via2, "第三次换乘站（层3），留空=最多两次换乘。填了才启用 4 段方案"),
            ("城际站", self.v_city, "这些站换乘只需 20 分钟缓冲（其余站 25~45 分）"),
        ]
        for i, (label, var, tip) in enumerate(row2):
            ttk.Label(box, text=label).grid(row=1, column=i * 2, padx=(10, 3), pady=4, sticky="e")
            e = ttk.Entry(box, textvariable=var, width=14)
            e.grid(row=1, column=i * 2 + 1, padx=(0, 6), sticky="w")
            e.bind("<Enter>", lambda ev, t=tip: self.hint(t))
        ttk.Label(box, text="最多换乘").grid(row=1, column=6, padx=(10, 3), sticky="e")
        mtc = ttk.Combobox(box, textvariable=self.v_mt, values=["1", "2", "3"], width=4)
        mtc.grid(row=1, column=7, sticky="w")
        mtc.bind("<Enter>", lambda ev: self.hint(
            "结果里最多展示几次换乘：1=只看直达/一次换乘（最快）；3=连三次换乘也算（要配三次换乘站）"))

        ttk.Label(box, text="路线 = 出发站 → 换乘城市 → 二次换乘 → 三次换乘 → 终点（中间层可跳过，最多 4 段）",
                  foreground="#888").grid(row=2, column=0, columnspan=10, sticky="w", padx=10, pady=(2, 6))

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
        bbox.pack(fill="x", padx=10, pady=(8, 2))
        self.busy = False
        for text, cmd in [("保存配置", self.on_save),
                          ("立即扫描（不推送）", self.on_scan),
                          ("测试微信推送", self.on_test),
                          ("同步到 GitHub（云端生效）", self.on_sync)]:
            ttk.Button(bbox, text=text, command=cmd).pack(side="left", padx=4)

        bbox2 = ttk.Frame(root)
        bbox2.pack(fill="x", padx=10, pady=(0, 8))
        for text, cmd, tip in [
                ("关闭云端监控", self.on_stop,
                 "停用云端定时任务：不再扫描、不再推微信。配置和仓库文件全部保留"),
                ("恢复云端监控", self.on_resume,
                 "把定时任务装回仓库，继续扫描并推微信"),
                ("查看云端状态", self.on_status,
                 "看云端监控当前是开着还是关着，以及最近一次运行时间")]:
            b = ttk.Button(bbox2, text=text, command=cmd)
            b.pack(side="left", padx=4)
            b.bind("<Enter>", lambda ev, t=tip: self.hint(t))

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
        cfg["via"] = split_codes(self.v_via.get())
        cfg["via2"] = split_codes(self.v_via2.get())
        cfg["city_stations"] = split_codes(self.v_city.get())
        try:
            cfg["max_transfers"] = max(1, min(3, int(str(self.v_mt.get()).strip() or 3)))
        except ValueError:
            cfg["max_transfers"] = 3
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


    # ---- 云端监控开关 ----

    def _cred(self):
        """取出 PAT 和仓库名，缺一样就弹提示并返回 (None, None)"""
        pat = self.v_pat.get().strip()
        repo = self.v_repo.get().strip()
        if not pat or "/" not in repo:
            messagebox.showerror("缺配置",
                                 "需要 GitHub PAT（ghp_ 开头）和仓库名（用户名/仓库名）。\n\n"
                                 "这两项填好并点过「保存配置」后就存在本机，不用每次重填。")
            return None, None
        return pat, repo

    @staticmethod
    def _wf_path(repo):
        return "/repos/%s/contents/%s" % (repo, urllib.parse.quote(WF_PATH))

    def on_status(self):
        pat, repo = self._cred()
        if not pat:
            return
        self.busy = True
        threading.Thread(target=self._status, args=(pat, repo), daemon=True).start()

    def _status(self, pat, repo):
        try:
            cur, err = gh("GET", self._wf_path(repo) + "?ref=main", pat)
            if err and "404" not in str(err):
                print("[云端] 查询失败：%s\n" % err)
                return
            exists = (not err) and isinstance(cur, dict) and cur.get("sha")
            if exists:
                print("[云端] 状态：开启中（仓库里有定时任务，正在扫描）")
                runs, e2 = gh("GET", "/repos/%s/actions/runs?per_page=1" % repo, pat)
                if not e2 and runs.get("workflow_runs"):
                    x = runs["workflow_runs"][0]
                    print("        最近一次运行：%s  %s\n"
                          % (x["created_at"], x["status"]))
            else:
                print("[云端] 状态：已关闭（仓库里没有定时任务，不会扫描也不会推送）\n")
        finally:
            self.busy = False

    def on_stop(self):
        pat, repo = self._cred()
        if not pat:
            return
        if not messagebox.askyesno(
                "确认关闭云端监控",
                "关闭后：\n"
                "  · 云端不再扫描余票\n"
                "  · 微信不再收到推送\n"
                "  · 配置和仓库文件全部保留，随时可恢复\n\n"
                "另外：本地那个「云端监控心跳」定时任务不归本工具管，\n"
                "需要在 WorkBuddy 的定时任务界面里单独暂停。\n\n"
                "确定关闭吗？"):
            return
        self.busy = True
        threading.Thread(target=self._stop, args=(pat, repo), daemon=True).start()

    def _stop(self, pat, repo):
        try:
            path = self._wf_path(repo)
            cur, err = gh("GET", path + "?ref=main", pat)
            if err or not (isinstance(cur, dict) and cur.get("sha")):
                print("[云端] 本来就是关闭状态，无需操作\n")
                return
            _, e = gh("DELETE", path, pat,
                      {"message": "stop monitoring", "sha": cur["sha"],
                       "branch": "main"})
            if e:
                print("[云端] 关闭失败：%s\n" % e)
            else:
                print("[云端] 已关闭：不再扫描、不再推微信。\n"
                      "       想恢复时点「恢复云端监控」即可。\n"
                      "       本地的「云端监控心跳」定时任务需另行暂停。\n")
        finally:
            self.busy = False

    def on_resume(self):
        pat, repo = self._cred()
        if not pat:
            return
        self.busy = True
        threading.Thread(target=self._resume, args=(pat, repo), daemon=True).start()

    def _resume(self, pat, repo):
        try:
            path = self._wf_path(repo)
            cur, err = gh("GET", path + "?ref=main", pat)
            if not err and isinstance(cur, dict) and cur.get("sha"):
                print("[云端] 本来就是开启状态，无需操作\n")
                return
            payload = {"message": "resume monitoring",
                       "content": base64.b64encode(
                           WF_TEMPLATE.encode("utf-8")).decode("ascii"),
                       "branch": "main"}
            _, e = gh("PUT", path, pat, payload)
            if e:
                print("[云端] 恢复失败：%s\n" % e)
            else:
                print("[云端] 已恢复：定时任务装回仓库，继续扫描并推微信。\n"
                      "       想立刻跑一轮，去仓库 Actions 页面点 Run workflow。\n")
        finally:
            self.busy = False


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
