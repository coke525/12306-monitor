#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""清掉仓库里的打包产物与死文件（用 Contents API，本机 git push 不通）"""
import json
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

# 这些前缀下的文件全部删（打包产物 / 交付目录，不该进版本库）
PREFIXES = ("dist/", "build_tmp/", "12306\u76d1\u63a7\u5de5\u5177/")
# 这些是开发过程留下的死文件，没有任何代码引用
DEAD = [
    "g7902_monitor.py", "g7902_log.txt", "g7902_snapshot.json",
    "_notify.py", "transfer_finder.py", "push_github.bat",
]


def call(method, path, token, payload=None):
    d = None
    h = {"Authorization": "Bearer " + token,
         "Accept": "application/vnd.github+json", "User-Agent": "cleanup"}
    if payload is not None:
        d = json.dumps(payload).encode("utf-8")
        h["Content-Type"] = "application/json"
    req = urllib.request.Request("https://api.github.com" + path, data=d,
                                 method=method, headers=h)
    try:
        b = urllib.request.urlopen(req, timeout=30, context=CTX).read()
        return (json.loads(b.decode("utf-8", "ignore")) if b.strip() else {}), None
    except urllib.error.HTTPError as e:
        return None, "HTTP %d" % e.code
    except Exception as e:
        return None, repr(e)[:120]


def main():
    token, full = sys.argv[1], sys.argv[2]
    tree, err = call("GET", "/repos/%s/git/trees/main?recursive=1" % full, token)
    if err:
        print("读取文件树失败:", err)
        return 1
    targets = []
    for it in tree.get("tree", []):
        if it["type"] != "blob":
            continue
        p = it["path"]
        if p.startswith(PREFIXES) or p in DEAD:
            targets.append((p, it["sha"]))
    print("待删除 %d 个文件" % len(targets))
    ok = 0
    for p, sha in targets:
        _, e = call("DELETE", "/repos/%s/contents/%s" % (full, urllib.parse.quote(p, safe="/")),
                    token, {"message": "cleanup: remove build artifacts and dead files",
                            "sha": sha, "branch": "main"})
        print(("  OK   " if not e else "  FAIL ") + p + ("" if not e else "  " + e))
        if not e:
            ok += 1
    print("完成 %d/%d" % (ok, len(targets)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
