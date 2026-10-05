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
import io
import json
import datetime
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


def _gh_api(method, path, payload=None, tries=3):
    """仓库文件读写（用于 github 模式的状态存储），失败自动重试"""
    tok = os.environ.get("GITHUB_PAT", "").strip()
    last = None
    for i in range(max(1, tries)):
        data = None
        h = {"Accept": "application/vnd.github+json", "User-Agent": "12306-monitor"}
        if tok:
            h["Authorization"] = "Bearer " + tok
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            h["Content-Type"] = "application/json"
        req = urllib.request.Request("https://api.github.com" + path, data=data,
                                     method=method, headers=h)
        try:
            raw = urllib.request.urlopen(req, timeout=30).read()
            return json.loads(raw.decode("utf-8", "ignore")) if raw.strip() else {}
        except Exception as e:      # noqa: BLE001
            last = e
            if i < tries - 1:
                import time
                time.sleep(2 * (i + 1))
    raise last


def _state_target():
    """返回 (模式, 仓库内路径, 本地临时路径)：
    github 模式的本地快照必须写到可写目录（云函数代码目录只读，/tmp 才可写）"""
    mode = (os.environ.get("STATE_MODE") or "auto").strip().lower()
    repo = os.environ.get("GITHUB_REPO", "").strip()
    have_pat = bool(os.environ.get("GITHUB_PAT", "").strip())
    if mode == "github" or (mode == "auto" and have_pat and repo):
        repo_path = os.environ.get("STATE_PATH") or "cloud_state.json"
        return "github", repo_path, _local_state_path(repo_path)
    return "file", "", os.environ.get("STATE_FILE") or os.path.join(BASE, "cloud_state.json")


def _local_state_path(repo_path):
    """云函数代码目录只读，本地快照放 /tmp；本机没有 /tmp 就退回代码目录"""
    name = repo_path.replace("/", "_").replace("\\", "_")
    for cand in (os.path.join("/tmp", name),
                 os.path.join(BASE, name),
                 os.path.join(os.getcwd(), name)):
        try:
            with open(cand, "a"):
                pass
            return cand
        except Exception:       # noqa: BLE001
            continue
    return os.path.join("/tmp", name)


_STATE_ERR = ""


def fetch_state(repo_path, local_path):
    """github 模式：把仓库里的快照拉到本地临时文件，让 tm 照常读写"""
    global _STATE_ERR
    import urllib.error
    try:
        cur = _gh_api("GET", "/repos/%s/contents/%s" % (
            os.environ["GITHUB_REPO"], urllib.parse.quote(repo_path)))
        if isinstance(cur, dict) and cur.get("content"):
            import base64
            with open(local_path, "wb") as fp:
                fp.write(base64.b64decode(cur["content"]))
            print("[cloud] 已从仓库读取状态快照 %s" % repo_path)
            return True
        _STATE_ERR = "内容为空: %s" % str(cur)[:120]
    except urllib.error.HTTPError as e:
        _STATE_ERR = "HTTP %d: %s" % (e.code, e.read().decode("utf-8", "ignore")[:150])
        if e.code != 404:
            print("[cloud] 读取状态失败 %s（按首次运行处理）" % _STATE_ERR)
    except Exception as e:      # noqa: BLE001
        _STATE_ERR = repr(e)[:150]
        print("[cloud] 读取状态失败 %s（按首次运行处理）" % _STATE_ERR)
    return False


def push_state(repo_path, local_path):
    """github 模式：把更新后的快照写回仓库"""
    global _STATE_ERR
    import base64
    try:
        with open(local_path, "rb") as fp:
            content = base64.b64encode(fp.read()).decode("ascii")
        api_path = "/repos/%s/contents/%s" % (
            os.environ["GITHUB_REPO"], urllib.parse.quote(repo_path))
        cur = {}
        try:
            cur = _gh_api("GET", api_path)
        except Exception:       # noqa: BLE001
            pass
        payload = {"message": "state: %s" % repo_path, "content": content,
                   "branch": os.environ.get("GITHUB_BRANCH") or "main"}
        if isinstance(cur, dict) and cur.get("sha"):
            payload["sha"] = cur["sha"]
        _gh_api("PUT", api_path, payload)
        print("[cloud] 状态快照已写回仓库 %s" % repo_path)
        return True
    except Exception as e:      # noqa: BLE001
        _STATE_ERR = repr(e)[:150]
        print("[cloud] 写回状态失败 %s（不影响本次推送）" % _STATE_ERR)
        return False


def _beijing_now():
    """云函数跑在 UTC，北京时间要自己 +8，否则心跳会晚 8 个小时"""
    return datetime.datetime.utcnow() + datetime.timedelta(hours=8)


def maybe_heartbeat(local_path):
    """每日心跳：北京时间过了设定钟点、且今天还没发过，就推一条「我还活着」。

    关机后你没法确认云端还跑不跑——只有发现新票才会推微信。
    这条让你每天固定收到一条，顺便验证推送链路没断。
    HEARTBEAT_HOUR=0 表示关闭。返回 (标题, html正文, 短文本) 或 None。
    """
    try:
        hour = int(os.environ.get("HEARTBEAT_HOUR", "8") or 0)
    except Exception:      # noqa: BLE001
        hour = 8
    if hour <= 0:
        return None
    now = _beijing_now()
    if now.hour < hour:
        return None

    snap = {}
    try:
        with io.open(local_path, encoding="utf-8") as fp:
            snap = json.load(fp) or {}
    except Exception:      # noqa: BLE001
        snap = {}
    today = now.strftime("%Y-%m-%d")
    if snap.get("_hb_date") == today:
        return None

    snap["_hb_date"] = today
    snap["_hb_at"] = now.strftime("%Y-%m-%d %H:%M")
    try:
        with io.open(local_path, "w", encoding="utf-8") as fp:
            json.dump(snap, fp, ensure_ascii=False, indent=2)
    except Exception:      # noqa: BLE001
        pass

    hits = len(snap.get("_hit_plans") or [])
    pairs = len(snap.get("_hit_pairs") or [])
    dates = list(getattr(tm, "DATES", []) or [])
    try:
        dest = tm.sname(str(tm.DEST).strip().upper())
    except Exception:      # noqa: BLE001
        dest = str(getattr(tm, "DEST", "") or "")
    daytxt = "、".join(str(d)[5:] for d in dates) or "（没配日期）"
    return ("12306 监控还活着（%s）" % today[5:],
            "<br>".join([
                "云端每分钟在跑，这条是每日报平安。",
                "监控日期：%s" % daytxt,
                "终点：%s" % dest,
                "当前命中你窗口的方案 %d 个，可同行 %d 个" % (hits, pairs),
                "出新票会另外单独推给你；没新票就不再打扰。"]),
            "[心跳] %s 云端正常｜%s｜终点%s｜命中%d个｜同行%d个｜有票会另推"
            % (today[5:], daytxt, dest, hits, pairs))


def run_once():
    """跑一轮：拉状态 → 扫描对比 → 推送 → 存状态。云函数直接调这个。
    返回状态信息字典，云函数入口会把它放进返回值，方便排查。"""
    global _STATE_ERR
    _STATE_ERR = ""
    mode, repo_path, local_path = _state_target()
    tm.SNAPSHOT = local_path
    _push.send = cloud_send
    info = {"mode": mode, "repo_path": repo_path, "local_path": local_path}

    argv = [sys.argv[0]] + sys.argv[1:]
    if mode == "github":
        print("[cloud] 状态模式：github（%s@%s，本地暂存 %s）"
              % (repo_path, os.environ.get("GITHUB_REPO"), local_path))
        got = fetch_state(repo_path, local_path)
        first_run = not got
        info["fetch"] = ("ok" if got else "FAIL: %s" % _STATE_ERR)[:200]
    else:
        first_run = not os.path.exists(local_path)
        info["fetch"] = "file:" + ("exists" if not first_run else "missing")

    if (first_run and "--showall" not in argv
            and os.environ.get("SKIP_FIRST_PUSH", "1") == "1"):
        # 第一次在云端跑，还没有基线，先把当前余票存下来，避免一上来刷屏
        argv.append("--nopush")
        info["first_run"] = True
        print("[cloud] 首次运行，只建立基线快照，不推送")
    sys.argv = argv
    rc = tm.main()

    info["rc"] = rc
    # 每日心跳（只有云端这条路径会发，本地 GUI 点扫描不会触发）
    try:
        hb = maybe_heartbeat(local_path)
        if hb:
            ok, msg = cloud_send(*hb)
            info["heartbeat"] = ("ok" if ok else "FAIL: %s" % msg)[:120]
            print("[cloud] 每日心跳：%s" % info["heartbeat"])
    except Exception as e:      # noqa: BLE001
        info["heartbeat"] = "ERR: %s" % str(e)[:80]
    if mode == "github" and os.path.exists(local_path):
        info["push"] = ("ok" if push_state(repo_path, local_path)
                        else ("FAIL: %s" % _STATE_ERR)[:200])
    return info


def main():
    info = run_once()
    rc = info.get("rc") if isinstance(info, dict) else info
    return rc if rc is not None else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
