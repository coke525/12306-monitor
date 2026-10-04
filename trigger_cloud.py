# -*- coding: utf-8 -*-
"""触发云端 GitHub Actions 跑一轮监控（本地心跳，弥补 GitHub 定时调度丢触发）。

用法:
  python trigger_cloud.py            # 触发一次
  python trigger_cloud.py --status   # 只看最近运行状态

PAT 从 push_config.json 的 github_pat 字段读（没有就用环境变量 GITHUB_PAT）。
PAT 需要 repo + workflow 权限。删掉 PAT 后本脚本会明确报错，不会误以为成功。
"""
import json
import os
import ssl
import sys
import time
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
    h = {"Accept": "application/vnd.github+json", "User-Agent": "trigger"}
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
    tok = get_token()
    if not tok:
        print("FAIL 没找到 GitHub PAT：在 push_config.json 里加 github_pat，"
              "或设环境变量 GITHUB_PAT")
        return 2

    if "--status" in sys.argv:
        runs, err = call("GET", "/repos/%s/actions/runs?per_page=3" % REPO, token=tok)
        if err:
            print("FAIL 查询失败:", err)
            return 1
        for x in runs.get("workflow_runs", []):
            print("  %s  %-9s %-9s" % (x["created_at"], x["event"], x["status"]))
        return 0

    # 先看看有没有正在跑的，避免重复触发
    runs, err = call("GET", "/repos/%s/actions/runs?per_page=3" % REPO, token=tok)
    if not err and runs.get("workflow_runs"):
        busy = [x for x in runs["workflow_runs"] if x["status"] in ("in_progress", "queued")]
        if busy:
            print("SKIP 已有任务在跑（%s），不重复触发" % busy[0]["status"])
            return 0

    _, err = call("POST", "/repos/%s/actions/workflows/%s/dispatches" % (REPO, WF),
                  {"ref": "main"}, token=tok)
    if err:
        print("FAIL 触发失败:", err)
        return 1
    print("OK 已触发云端扫描（%s）。任务内部连续扫约 27 分钟。" % REPO)

    time.sleep(8)
    runs, err = call("GET", "/repos/%s/actions/runs?per_page=1" % REPO, token=tok)
    if not err and runs.get("workflow_runs"):
        x = runs["workflow_runs"][0]
        print("    最新运行:", x["created_at"], x["status"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
