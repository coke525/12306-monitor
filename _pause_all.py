# -*- coding: utf-8 -*-
"""全部暂停：腾讯云定时触发器关掉 + GitHub Actions 工作流禁用。

配置和代码都留着，随时能一键恢复（python _resume_all.py）。
"""
import io
import json
import os
import ssl
import sys
import urllib.error
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import deploy_tencent as dt      # noqa: E402

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
API = "https://api.github.com"


def gh(method, path, token, payload=None):
    data = None
    h = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
         "User-Agent": "12306-monitor", "X-GitHub-Api-Version": "2022-11-28"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(API + path, data=data, method=method)
    for k, v in h.items():
        req.add_header(k, v)
    try:
        r = urllib.request.urlopen(req, timeout=40, context=CTX)
        raw = r.read()
        return (json.loads(raw.decode("utf-8", "ignore")) if raw.strip() else {}), r.status
    except urllib.error.HTTPError as e:
        b = e.read().decode("utf-8", "ignore")
        try:
            return json.loads(b), e.code
        except Exception:
            return {"message": b[:200]}, e.code


def main():
    with io.open(os.path.join(HERE, "12306监控工具", "push_config.json"),
                 encoding="utf-8") as fp:
        c = json.load(fp)
    sid, skey = c.get("tencent_sid", ""), c.get("tencent_skey", "")
    region = c.get("tencent_region") or "ap-guangzhou"
    pat, repo = c.get("github_pat", ""), c.get("github_repo") or "coke525/12306-monitor"

    print("=== 1. 腾讯云函数定时触发器 ===")
    on, info = dt.trigger_on(region, sid, skey)
    print("   当前状态：%s（%s）" % ("开着" if on else "关着" if on is False else "查不到", info))
    for i in range(3):                      # 刚部署完偶尔要等状态同步
        ok, err = dt.set_trigger(region, sid, skey, False)
        if ok:
            print("   已关闭每分钟触发器")
            break
        print("   第%d次失败：%s" % (i + 1, err))
    on2, _ = dt.trigger_on(region, sid, skey)
    print("   复查：%s" % ("仍然开着，需要手动处理" if on2 else "已停（不会再每分钟跑）"))

    print("\n=== 2. GitHub Actions 工作流 ===")
    r, code = gh("GET", "/repos/%s/actions/workflows" % repo, pat)
    if code >= 400:
        print("   查不到工作流（%s %s），本来就停着" % (code, r.get("message", "")))
    else:
        wfs = r.get("workflows") or []
        if not wfs:
            print("   仓库里没有工作流文件")
        for w in wfs:
            name, state = w.get("name"), w.get("state")
            wid = w.get("id")
            print("   %-16s 状态=%s" % (name, state))
            if state == "active":
                _, c2 = gh("PUT", "/repos/%s/actions/workflows/%s/disable" % (repo, wid), pat)
                print("     -> 禁用：%s" % ("OK" if c2 < 300 else "返回 %s" % c2))
            else:
                print("     -> 已经是禁用状态")

    print("\n=== 3. 本机 ===")
    print("   本机没有常驻的监控进程，界面关掉就是停；云端已停。")
    print("\n全部暂停完成。要恢复：改回界面里的按钮，或跑 python _resume_all.py")


if __name__ == "__main__":
    main()
