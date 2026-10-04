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
  STATE_FILE        快照路径，默认 ./cloud_state.json
  SKIP_FIRST_PUSH   首次运行（无快照）只建基线、不推送，默认 1

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


def main():
    state = os.environ.get("STATE_FILE") or os.path.join(BASE, "cloud_state.json")
    tm.SNAPSHOT = state
    _push.send = cloud_send

    argv = [sys.argv[0]] + sys.argv[1:]
    first_run = not os.path.exists(state)
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
