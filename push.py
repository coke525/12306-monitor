# -*- coding: utf-8 -*-
"""
微信推送（多通道，哪个配好用哪个）

通道一：PushPlus  https://www.pushplus.plus
    1. 微信扫码登录 pushplus.plus，首页会显示 token
    2. 必须完成实名认证才能发送（否则返回"账户未进行实名认证"）
    3. 把 token 填进 push_config.json 的 "token"

通道二：WxPusher  https://wxpusher.zjiecode.com   （不需要实名，扫码即用）
    1. 微信扫码登录 wxpusher.zjiecode.com
    2. 后台「应用管理」新建一个应用，拿到 appToken（AT_ 开头）
    3. 首页/「用户」页面拿到自己的 UID（UID_ 开头）
    4. 填进 push_config.json 的 "wxpusher_app_token" 和 "wxpusher_uid"

两个都填就两条都发（互为备份）。都没填则静默跳过，不影响监控。
"""
import json
import os
import re
import urllib.parse
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(BASE_DIR, "push_config.json")

PUSHPLUS_API = "http://www.pushplus.plus/send"
WXPUSHER_API = "https://wxpusher.zjiecode.com/api/send/message"


SPT_MAX = 160          # 极简推送纯文本最大字符数（URL 长度硬限制）


def strip_html(s):
    """极简推送走 GET，内容要纯文本，把 HTML 标签去掉"""
    s = re.sub(r"<br\s*/?>", "\n", s or "")
    s = re.sub(r"</p\s*>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def wxpusher_spt(spt, title, content, short=None):
    """WxPusher 极简推送：不需要 appToken + UID，一个 SPT 直接发给自己
    接口：GET /api/send/message/{SPT}/{内容}
    """
    text = (short or (title + "\n" + strip_html(content))).strip()
    # 极简推送走 GET，URL 太长会被服务端直接断连（WinError 10054），必须截断
    if len(text) > SPT_MAX:
        text = text[:SPT_MAX] + "…（更多请查看板）"
    url = "%s/%s/%s" % (WXPUSHER_API, spt, urllib.parse.quote(text))
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "Mozilla/5.0 12306-monitor")
    r = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")
    return json.loads(r)


def load_cfg():
    try:
        with open(CFG, "r", encoding="utf-8") as fp:
            return json.load(fp) or {}
    except Exception:
        return {}


def _post(url, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body)
    req.add_header("Content-Type", "application/json")
    r = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")
    return json.loads(r)


def send(title, content, short=None):
    """content 支持简单 HTML；short 是给极简推送用的短纯文本（URL 有长度硬限制）。
    返回 (是否至少一条成功, 说明)"""
    cfg = load_cfg()
    msgs = []

    token = (cfg.get("token") or "").strip()
    if token:
        try:
            j = _post(PUSHPLUS_API, {"token": token, "title": title,
                                     "content": content, "template": "html"})
            if j.get("code") == 200:
                msgs.append("PushPlus: 已推送")
            else:
                msgs.append("PushPlus 失败: %s" % j.get("msg", str(j)[:100]))
        except Exception as e:      # noqa: BLE001
            msgs.append("PushPlus 异常: %s" % e)

    app = (cfg.get("wxpusher_app_token") or "").strip()
    uid = (cfg.get("wxpusher_uid") or "").strip()
    if app and uid:
        try:
            j = _post(WXPUSHER_API, {"appToken": app, "summary": title,
                                     "content": content, "contentType": 2,
                                     "uids": [uid]})
            if (j.get("data") or j.get("success")) and j.get("code") == 1000:
                msgs.append("WxPusher: 已推送")
            else:
                msgs.append("WxPusher 失败: %s" % j.get("msg", str(j)[:100]))
        except Exception as e:      # noqa: BLE001
            msgs.append("WxPusher 异常: %s" % e)

    spt = (cfg.get("wxpusher_spt") or "").strip()
    if spt:
        try:
            j = wxpusher_spt(spt, title, content, short=short)
            if j.get("code") == 1000 or j.get("success"):
                msgs.append("WxPusher极简: 已推送")
            else:
                msgs.append("WxPusher极简 失败: %s" % j.get("msg", str(j)[:100]))
        except Exception as e:      # noqa: BLE001
            msgs.append("WxPusher极简 异常: %s" % e)

    if not msgs:
        return False, "未配置任何推送通道，跳过（填 push_config.json）"
    ok = any("已推送" in m for m in msgs)
    return ok, " | ".join(msgs)


if __name__ == "__main__":
    ok, msg = send("12306 监控测试", "如果你在微信里收到这条，说明推送通道已打通。")
    print(msg)
