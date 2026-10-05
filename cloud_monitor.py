#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
云端入口（GitHub Actions / 云函数 / 任意服务器 通用）
=====================================================

复用 train_monitor.py 的全部扫描与方案拼装逻辑，只替换两处：
  1) 推送凭据改为读环境变量（token 绝不进代码、绝不进仓库）
  2) 快照文件路径可指定（GitHub Actions 里把 cloud_state.json
     提交回仓库，实现跨次运行的"上次状态"记忆）

环境变量：
  WXPUSHER_SPT      WxPusher 极简推送 token（SPT_ 开头）  <- 目前主力通道
  PUSHPLUS_TOKEN    PushPlus token（可选，需实名）
  WXPUSHER_TOKEN    WxPusher 应用 appToken（AT_ 开头，可选）
  WXPUSHER_UID      WxPusher 用户 UID（可选，配上面一起用）
  SKIP_FIRST_PUSH   首次运行（无快照）只建基线、不推送，默认 1

状态存储（跨次运行记住"上次余票"，否则每次都当新票刷屏）：
  STATE_MODE        auto / file / github，默认 auto
                    auto = 配了 GITHUB_PAT 就用 github，否则用本地文件
  STATE_FILE        file 模式下的快照路径，默认 ./cloud_state.json
  GITHUB_PAT        github 模式下读写仓库用（PAT 或 GITHUB_TOKEN 都行）
  GITHUB_REPO       github 模式下的仓库，如 coke525/12306-monitor
  STATE_PATH        github 模式下仓库里的文件路径，默认 cloud_state.json

为什么需要 github 模式：云函数（腾讯云 SCF / 阿里云 FC）是无状态的，
容器回收后 /tmp 就清空了，本地文件存不住状态；改成读写仓库里的文件即可通用。
GitHub Actions 继续用 file 模式（配合 git 提交），行为不变。

用法：
  python cloud_monitor.py              # 扫描 + 对比 + 推送
  python cloud_monitor.py --showall    # 只列出当前所有车次/方案，不推送
"""

import os
import sys
import json
import urllib.parse
import urllib.request

# 冻结成 exe 后用 exe 所在目录（打包后 __file__ 指向临时的 _MEIPASS）
BASE = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) \
    else os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import train_monitor as tm   # noqa: E402
import push as _push         # noqa: E402

PUSHPLUS_API = "http://www.pushplus.plus/send"
WXPUSHER_API = "https://wxpusher.zjiecode.com/api/send/message"
SPT_MAX = 160


def _post(url, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body)
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "Mozilla/5.0 12306-monitor")
    r = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")
    return json.loads(r)


def _spt_send(spt, text):
    """WxPusher 极简推送：GET，内容硬限制很短"""
    text = (text or "").strip()
    if len(text) > SPT_MAX:
        text = text[:SPT_MAX] + "…"
    url = "%s/%s/%s" % (WXPUSHER_API, spt, urllib.parse.quote(text))
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "Mozilla/5.0 12306-monitor")
    r = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")
    return json.loads(r)


def cloud_send(title, content, short=None):
    """云端推送：全走环境变量；一个都没配就退回本地 push_config.json"""
    msgs = []

    token = os.environ.get("PUSHPLUS_TOKEN", "").strip()
    if token:
        try:
            j = _post(PUSHPLUS_API, {"token": token, "title": title,
                                     "content": content, "template": "html"})
            msgs.append("PushPlus: 已推送" if j.get("code") == 200
                        else "PushPlus 失败: %s" % j.get("msg", str(j)[:100]))
        except Exception as e:      # noqa: BLE001
            msgs.append("PushPlus 异常: %s" % e)

    app = os.environ.get("WXPUSHER_TOKEN", "").strip()
    uid = os.environ.get("WXPUSHER_UID", "").strip()
    if app and uid:
        try:
            j = _post(WXPUSHER_API, {"appToken": app, "summary": title,
                                     "content": content, "contentType": 2,
                                     "uids": [uid]})
            msgs.append("WxPusher: 已推送" if j.get("code") == 1000
                        else "WxPusher 失败: %s" % j.get("msg", str(j)[:100]))
        except Exception as e:      # noqa: BLE001
            msgs.append("WxPusher 异常: %s" % e)

    spt = os.environ.get("WXPUSHER_SPT", "").strip()
    if spt:
        try:
            text = short or (title + "\n" + _push.strip_html(content))
            j = _spt_send(spt, text)
            msgs.append("WxPusher极简: 已推送" if (j.get("code") == 1000 or j.get("success"))
                        else "WxPusher极简 失败: %s" % j.get("msg", str(j)[:100]))
        except Exception as e:      # noqa: BLE001
            msgs.append("WxPusher极简 异常: %s" % e)

    if not msgs:
        # 没配任何环境变量 -> 退回本地配置（方便在本机测试）
        return _push.send(title, content, short=short)

    ok = any("已推送" in m for m in msgs)
    return ok, " | ".join(msgs)


def _gh_api(method, path, payload=None):
    """仓库文件读写（用于 github 模式的状态存储）"""
    tok = os.environ.get("GITHUB_PAT", "").strip()
    data = None
    h = {"Accept": "application/vnd.github+json", "User-Agent": "12306-monitor"}
    if tok:
        h["Authorization"] = "Bearer " + tok
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        h["Content-Type"] = "application/json"
    req = urllib.request.Request("https://api.github.com" + path, data=data,
                                 method=method, headers=h)
    raw = urllib.request.urlopen(req, timeout=30).read()
    return json.loads(raw.decode("utf-8", "ignore")) if raw.strip() else {}


def _state_target():
    """返回 (模式, 路径)：决定状态存在本地文件还是仓库里"""
    mode = (os.environ.get("STATE_MODE") or "auto").strip().lower()
    repo = os.environ.get("GITHUB_REPO", "").strip()
    have_pat = bool(os.environ.get("GITHUB_PAT", "").strip())
    if mode == "github" or (mode == "auto" and have_pat and repo):
        return "github", os.environ.get("STATE_PATH") or "cloud_state.json"
    return "file", os.environ.get("STATE_FILE") or os.path.join(BASE, "cloud_state.json")


def fetch_state(path):
    """github 模式：把仓库里的快照拉到本地临时文件，让 tm 照常读写"""
    import urllib.error
    try:
        cur = _gh_api("GET", "/repos/%s/contents/%s" % (
            os.environ["GITHUB_REPO"], urllib.parse.quote(path)))
        if isinstance(cur, dict) and cur.get("content"):
            import base64
            with open(path, "wb") as fp:
                fp.write(base64.b64decode(cur["content"]))
            print("[cloud] 已从仓库读取状态快照 %s" % path)
            return True
    except urllib.error.HTTPError as e:
        if e.code != 404:
            print("[cloud] 读取状态失败 HTTP %d（按首次运行处理）" % e.code)
    except Exception as e:      # noqa: BLE001
        print("[cloud] 读取状态失败 %s（按首次运行处理）" % repr(e)[:80])
    return False


def push_state(path):
    """github 模式：把更新后的快照写回仓库"""
    import base64
    try:
        with open(path, "rb") as fp:
            content = base64.b64encode(fp.read()).decode("ascii")
        api_path = "/repos/%s/contents/%s" % (
            os.environ["GITHUB_REPO"], urllib.parse.quote(path))
        cur = {}
        try:
            cur = _gh_api("GET", api_path)
        except Exception:       # noqa: BLE001
            pass
        payload = {"message": "state: %s" % path, "content": content,
                   "branch": os.environ.get("GITHUB_BRANCH") or "main"}
        if isinstance(cur, dict) and cur.get("sha"):
            payload["sha"] = cur["sha"]
        _gh_api("PUT", api_path, payload)
        print("[cloud] 状态快照已写回仓库 %s" % path)
        return True
    except Exception as e:      # noqa: BLE001
        print("[cloud] 写回状态失败 %s（不影响本次推送）" % repr(e)[:100])
        return False


def run_once():
    """跑一轮：拉状态 → 扫描对比 → 推送 → 存状态。云函数直接调这个。"""
    mode, state = _state_target()
    tm.SNAPSHOT = state
    _push.send = cloud_send

    argv = [sys.argv[0]] + sys.argv[1:]
    if mode == "github":
        print("[cloud] 状态模式：github（%s@%s）"
              % (state, os.environ.get("GITHUB_REPO")))
        got = fetch_state(state)
        first_run = not got
    else:
        first_run = not os.path.exists(state)

    if (first_run and "--showall" not in argv
            and os.environ.get("SKIP_FIRST_PUSH", "1") == "1"):
        # 第一次在云端跑，还没有基线，先把当前余票存下来，避免一上来刷屏
        argv.append("--nopush")
        print("[cloud] 首次运行，只建立基线快照，不推送")
    sys.argv = argv
    rc = tm.main()

    if mode == "github" and os.path.exists(state):
        push_state(state)
    return rc


def main():
    return run_once()
    if (first_run and "--showall" not in argv
            and os.environ.get("SKIP_FIRST_PUSH", "1") == "1"):
        # 第一次在云端跑，还没有基线，先把当前余票存下来，避免一上来刷屏
        argv.append("--nopush")
        print("[cloud] 首次运行，只建立基线快照，不推送")
    sys.argv = argv
    return tm.main()


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
