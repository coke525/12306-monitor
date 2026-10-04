# -*- coding: utf-8 -*-
"""一键关闭 / 恢复云端 12306 监控。

用法:
  python stop_monitor.py status    # 看当前是开还是关
  python stop_monitor.py off       # 关闭（停用云端定时任务，配置全保留）
  python stop_monitor.py on        # 恢复

实现原理:
- 关 = 从仓库删掉 .github/workflows/ticket.yml（GitHub 没这个文件就不会再定时触发）
- 开 = 从本地的 .github/workflows/ticket.yml 原样传回去

本地那份文件永远在，所以随时能恢复。monitor_config.json 和整个过程无关，不会被删。
另外：本地那个「云端监控心跳」定时任务不归本脚本管，需要另外暂停。
"""
import base64
import json
import os
import ssl
import sys
import urllib.parse
import urllib.request
import urllib.error

BASE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(BASE, "push_config.json")
REPO = "coke525/12306-monitor"
WF = "ticket.yml"

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def get_token():
    tok = os.environ.get("GITHUB_PAT", "").strip()
    if tok:
        return tok
    try:
        with open(CFG, encoding="utf-8") as f:
            return (json.load(f).get("github_pat") or "").strip()
    except Exception:
        return ""


def call(method, path, payload=None, token=""):
    data = None
    h = {"Accept": "application/vnd.github+json", "User-Agent": "stop-monitor"}
    if token:
        h["Authorization"] = "Bearer " + token
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        h["Content-Type"] = "application/json"
    req = urllib.request.Request("https://api.github.com" + path,
                                 data=data, method=method, headers=h)
    try:
        raw = urllib.request.urlopen(req, timeout=40, context=CTX).read()
        return (json.loads(raw.decode("utf-8", "ignore")) if raw.strip() else {}), None
    except urllib.error.HTTPError as e:
        return None, "HTTP %d %s" % (e.code, e.read().decode("utf-8", "ignore")[:180])
    except Exception as e:
        return None, repr(e)[:180]


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("status", "off", "on"):
        print(__doc__)
        return 1
    mode = sys.argv[1]
    tok = get_token()
    if not tok:
        print("FAIL 没找到 GitHub PAT：push_config.json 里加 github_pat，"
              "或设环境变量 GITHUB_PAT")
        return 2

    path = ".github/workflows/" + WF
    wf_path = "/repos/%s/contents/%s" % (REPO, urllib.parse.quote(path))
    cur, cerr = call("GET", wf_path, token=tok)
    exists = not cerr and isinstance(cur, dict) and cur.get("sha")

    if mode == "status":
        if exists:
            print("云端定时任务：【开启中】")
            runs, e2 = call("GET", "/repos/%s/actions/runs?per_page=1" % REPO, token=tok)
            if not e2 and runs.get("workflow_runs"):
                x = runs["workflow_runs"][0]
                print("最近一次运行：%s  %s" % (x["created_at"], x["status"]))
        else:
            print("云端定时任务：【已关闭】（仓库里没有 workflow 文件）")
        print("本地备份存在：" + ("是" if os.path.exists(os.path.join(BASE, path))
                             else "否（无法恢复！）"))
        return 0

    if mode == "off":
        if not exists:
            print("本来就是关闭状态，无需操作")
            return 0
        _, err = call("DELETE", wf_path, {"message": "stop monitoring",
                                          "sha": cur["sha"], "branch": "main"}, token=tok)
        if err:
            print("FAIL 删除失败:", err)
            return 1
    else:
        local = os.path.join(BASE, path)
        if not os.path.exists(local):
            print("FAIL 本地没有 %s，恢复不了" % path)
            return 1
        with open(local, "rb") as f:
            content = base64.b64encode(f.read()).decode("ascii")
        payload = {"message": "resume monitoring", "content": content, "branch": "main"}
        if exists:
            payload["sha"] = cur["sha"]
        _, err = call("PUT", wf_path, payload, token=tok)
        if err:
            print("FAIL 上传失败:", err)
            return 1

    if mode == "on":
        print("OK 已恢复：云端定时任务回来了，继续扫描并推微信。")
        print("   想立刻跑一轮：python trigger_cloud.py")
    else:
        print("OK 已关闭：云端不再扫描、不再推微信。")
        print("   monitor_config.json 和仓库其他文件都还在，")
        print("   随时 python stop_monitor.py on 就能恢复。")
        print("   另外本地那个「云端监控心跳」定时任务也要在 WorkBuddy 里暂停。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
