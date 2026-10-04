#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""git push 走 github.com 在本机不通，改用 GitHub Contents API 逐个写文件。

用法： python push_via_api.py <PAT> [repo]
"""
import os
import sys
import ssl
import json
import base64
import subprocess
import urllib.parse
import urllib.request
import urllib.error

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
API = "https://api.github.com"
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def api(method, path, token, payload=None):
    data = None
    h = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
         "User-Agent": "12306-monitor", "X-GitHub-Api-Version": "2022-11-28"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(API + path, data=data, method=method)
    for k, v in h.items():
        req.add_header(k, v)
    try:
        r = urllib.request.urlopen(req, timeout=40, context=CTX)
        return json.loads(r.read().decode("utf-8", "ignore")), r.status
    except urllib.error.HTTPError as e:
        b = e.read().decode("utf-8", "ignore")
        try:
            return json.loads(b), e.code
        except Exception:
            return {"message": b[:200]}, e.code


def main():
    token = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("GITHUB_PAT", "")).strip()
    repo_name = (sys.argv[2] if len(sys.argv) > 2 else "12306-monitor").strip()
    me, _ = api("GET", "/user", token)
    owner = me.get("login")
    if not owner:
        print("PAT 无效：%s" % me)
        return 1
    full = "%s/%s" % (owner, repo_name)

    info, _ = api("GET", "/repos/" + full, token)
    branch = info.get("default_branch") or "main"
    print("仓库 %s  默认分支 %s" % (full, branch))

    out = subprocess.run(["git", "-c", "core.quotePath=false", "ls-files"],
                         cwd=BASE, capture_output=True,
                         text=True, encoding="utf-8", errors="replace")
    files = [f.strip() for f in (out.stdout or "").splitlines() if f.strip()]
    print("待写入 %d 个文件" % len(files))

    ok_n = 0
    for rel0 in files:
        rel = rel0.strip('"')          # git 可能给中文路径加引号
        rel_url = urllib.parse.quote(rel, safe="/")
        p = os.path.join(BASE, rel)
        if not os.path.isfile(p):
            continue
        with open(p, "rb") as fp:
            content = base64.b64encode(fp.read()).decode()
        payload = {"message": "add " + rel, "content": content}
        cur, code = api("GET", "/repos/%s/contents/%s?ref=%s" % (full, rel_url, branch), token)
        if code < 400 and isinstance(cur, dict) and cur.get("sha"):
            payload["sha"] = cur["sha"]
            payload["message"] = "update " + rel
        r, code = api("PUT", "/repos/%s/contents/%s" % (full, rel_url), token, payload)
        if code < 300:
            ok_n += 1
            print("  OK   %s" % rel)
        else:
            print("  FAIL %s -> %s %s" % (rel, code, r.get("message", str(r)[:120])))
    print("完成 %d/%d" % (ok_n, len(files)))
    return 0 if ok_n == len(files) else 1


if __name__ == "__main__":
    sys.exit(main())
