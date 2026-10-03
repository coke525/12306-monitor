#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一条龙：建私有仓库 -> 推代码 -> 配 Secret -> 开写权限 -> 触发跑一次 -> 取回日志

用法：
    python setup_github.py <你的GitHub PAT> [仓库名]

PAT 生成地址（scopes 已预填 repo + workflow）：
    https://github.com/settings/tokens/new?scopes=repo,workflow&description=12306-monitor
选 classic token，勾 repo 和 workflow，Generate 后把 ghp_ 开头那串粘过来。
"""
import os
import sys
import ssl
import json
import time
import base64
import subprocess
import urllib.request
import urllib.error

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
API = "https://api.github.com"
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def api(method, path, token, payload=None, raw=False, headers=None):
    url = API + path
    data = None
    h = {
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "User-Agent": "12306-monitor-setup",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if headers:
        h.update(headers)
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in h.items():
        req.add_header(k, v)
    try:
        r = urllib.request.urlopen(req, timeout=30, context=CTX)
        body = r.read()
        if raw:
            return body, r.status
        txt = body.decode("utf-8", "ignore").strip()
        return (json.loads(txt) if txt else {}), r.status
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        try:
            return json.loads(body), e.code
        except Exception:
            return {"message": body[:300]}, e.code


def run(cmd, **kw):
    print("  $ " + " ".join(cmd[:3]) + " ...")
    p = subprocess.run(cmd, cwd=BASE, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", **kw)
    if p.returncode != 0:
        print("    !! rc=%d" % p.returncode)
        if p.stdout:
            print(p.stdout[-1500:])
        if p.stderr:
            print(p.stderr[-1500:])
    return p.returncode == 0, (p.stdout or "") + (p.stderr or "")


def main():
    token = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("GITHUB_PAT", "")).strip()
    repo_name = (sys.argv[2] if len(sys.argv) > 2 else "12306-monitor").strip()
    if not token:
        print("缺少 PAT。用法：python setup_github.py <PAT> [仓库名]")
        return 1

    print("[1/7] 校验 PAT ...")
    me, code = api("GET", "/user", token)
    if code >= 400 or "login" not in me:
        print("  PAT 无效或权限不足：%s" % me.get("message", me))
        return 1
    owner = me["login"]
    print("  GitHub 账号：%s" % owner)

    print("[2/7] 创建私有仓库 %s ..." % repo_name)
    repo, code = api("POST", "/user/repos", token,
                     {"name": repo_name, "private": True, "auto_init": False,
                      "description": "12306 余票监控（GitHub Actions 定时扫描 + 微信推送）"})
    if code == 422:
        print("  仓库已存在，直接复用")
    elif code >= 400:
        print("  创建失败：%s" % repo.get("message", repo))
        return 1
    else:
        print("  已创建：%s" % repo["html_url"])
    full = "%s/%s" % (owner, repo_name)

    print("[3/7] 推送代码 ...")
    if os.environ.get("SKIP_PUSH") == "1":
        print("  SKIP_PUSH=1，跳过（代码已用 API 写入）")
        run(["git", "remote", "remove", "origin"])
        run(["git", "remote", "add", "origin", "https://github.com/%s.git" % full])
    else:
        push_url = "https://%s:%s@github.com/%s.git" % (owner, token, full)
        run(["git", "remote", "remove", "origin"])
        run(["git", "remote", "add", "origin", push_url])
        run(["git", "branch", "-M", "main"])
        ok, out = run(["git", "push", "-u", "origin", "main"])
        if not ok:
            print("  推送失败，检查 PAT 是否勾了 repo 权限")
            return 1
        print("  代码已推送")
        # 把带 token 的 URL 换掉，别把凭据留在 .git/config 里
        run(["git", "remote", "set-url", "origin", "https://github.com/%s.git" % full])

    print("[4/7] 配置 Actions Secret（WXPUSHER_SPT）...")
    cfg = {}
    try:
        with open(os.path.join(BASE, "push_config.json"), "r", encoding="utf-8") as fp:
            cfg = json.load(fp) or {}
    except Exception:
        pass
    spt = (cfg.get("wxpusher_spt") or os.environ.get("WXPUSHER_SPT", "")).strip()
    secrets = {}
    if spt:
        secrets["WXPUSHER_SPT"] = spt
    for k in ("PUSHPLUS_TOKEN", "WXPUSHER_TOKEN", "WXPUSHER_UID"):
        v = (cfg.get(k) or os.environ.get(k, "")).strip()
        if v:
            secrets[k] = v
    if not secrets:
        print("  没找到任何推送凭据，跳过（之后需手动加 Secret）")
    else:
        try:
            from nacl import encoding as nacl_enc
            from nacl.public import PublicKey, SealedBox
        except Exception:
            print("  缺少 pynacl，无法加密 Secret，改为手动提示")
            secrets = {}
        for name, value in secrets.items():
            pub, code = api("GET", "/repos/%s/actions/secrets/public-key" % full, token)
            if code >= 400:
                print("  取公钥失败：%s" % pub.get("message", pub))
                continue
            box = SealedBox(PublicKey(base64.b64decode(pub["key"])))
            enc = base64.b64encode(box.encrypt(value.encode("utf-8"))).decode()
            r, code = api("PUT", "/repos/%s/actions/secrets/%s" % (full, name), token,
                          {"encrypted_value": enc, "key_id": pub["key_id"]})
            print("  %s: %s" % (name, "已写入" if code < 300 else "失败 %s" % r.get("message", r)))

    print("[5/7] 打开 Actions 写权限 ...")
    r, code = api("PUT", "/repos/%s/actions/permissions/workflow" % full, token,
                  {"default_workflow_permissions": "write",
                   "can_approve_pull_request_reviews": True})
    print("  %s" % ("已设为 read/write" if code < 300 else "失败：%s" % r.get("message", r)))

    print("[6/7] 手动触发一次运行 ...")
    r, code = api("POST", "/repos/%s/actions/workflows/ticket.yml/dispatches" % full,
                  token, {"ref": "main"})
    if code >= 300:
        print("  触发失败（%s）：%s" % (code, r.get("message", r)))
        print("  可能 Actions 还没初始化，等 30 秒到仓库页面手动 Run workflow")
        return 0

    print("[7/7] 等待运行结果 ...")
    run_id = None
    for _ in range(20):
        time.sleep(5)
        runs, code = api("GET", "/repos/%s/actions/runs?per_page=3" % full, token)
        lst = (runs.get("workflow_runs") or []) if isinstance(runs, dict) else []
        if lst:
            run_id = lst[0]["id"]
            st, concl = lst[0].get("status"), lst[0].get("conclusion")
            print("  status=%s conclusion=%s" % (st, concl))
            if st == "completed":
                break
    if not run_id:
        print("  没能拿到运行记录，去网页看：https://github.com/%s/actions" % full)
        return 0

    logs, code = api("GET", "/repos/%s/actions/runs/%d/logs" % (full, run_id),
                     token, raw=True)
    if code < 300 and isinstance(logs, (bytes, bytearray)):
        txt = logs.decode("utf-8", "ignore")
        keep = []
        for line in txt.splitlines():
            if any(k in line for k in ("查询", "有票", "扫描", "命中", "推送", "WARN", "Error", "cloud")):
                keep.append(line)
        print("  ---- 关键日志 ----")
        for line in keep[:60]:
            print("  " + line.strip()[:160])
        if not keep:
            print("  （日志里没匹配到关键行，完整日志见网页）")
    else:
        print("  取日志失败，去这里看：https://github.com/%s/actions/runs/%d" % (full, run_id))

    print("\n完成：https://github.com/%s" % full)
    print("以后每 5 分钟自动扫一次（北京 07:00-23:55），有票直接推微信。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
