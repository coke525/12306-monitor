# -*- coding: utf-8 -*-
"""把 12306 监控部署到腾讯云函数 SCF（全自动，一条命令搞定）。

纯标准库实现（hmac/hashlib/urllib），不依赖任何第三方包 ——
腾讯云官方 SDK 的两个子包会争抢 tencentcloud/__init__.py，在受限环境里装不上。

用法：
  python deploy_tencent.py <SecretId> <SecretKey> [地域]

例（地域默认广州）：
  python deploy_tencent.py AKIDxxxx xxxxKey ap-guangzhou

依次做：
  1. 用本地最新代码 + monitor_config.json 打成 zip
  2. 创建（或复用）名为 12306-monitor 的云函数
  3. 配好环境变量（微信推送 + GitHub 状态存储）
  4. 挂上每 1 分钟执行一次的定时触发器
  5. 立刻调用一次验证，并把云端日志打出来

凭据只从命令行参数读，不写进任何文件、不进仓库。
"""
import base64
import hashlib
import hmac
import io
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
import zipfile

BASE = os.path.dirname(os.path.abspath(__file__))

CODE_FILES = ["cloud_function.py", "cloud_monitor.py",
              "train_monitor.py", "push.py", "monitor_config.json"]

HOST = "scf.tencentcloudapi.com"
SERVICE = "scf"
VERSION = "2018-04-16"
FUNC_NAME = "ticket-monitor"
HANDLER = "cloud_function.main_handler"
RUNTIME = "Python3.10"
TIMEOUT = 120
MEMORY = 128
CRON_7 = "0 */1 * * * * *"      # 腾讯云 cron 是 7 位（秒 分 时 日 月 星期 年）

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


# ---------------------------------------------------------------- 签名

def _sign(key, msg):
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def call_api(action, region, sid, skey, payload,
             host=HOST, service=SERVICE, version=VERSION):
    """腾讯云 TC3-HMAC-SHA256 签名 + POST（可指定其它服务）"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ts = int(time.time())
    date = time.strftime("%Y-%m-%d", time.gmtime(ts))

    ct = "application/json"
    canonical_headers = "content-type:%s\nhost:%s\nx-tc-action:%s\n" % (
        ct, host, action.lower())
    signed_headers = "content-type;host;x-tc-action"
    hashed = hashlib.sha256(body).hexdigest()
    canonical = "\n".join(["POST", "/", "", canonical_headers,
                           signed_headers, hashed])

    algo = "TC3-HMAC-SHA256"
    to_sign = "\n".join([algo, str(ts),
                         "%s/%s/tc3_request" % (date, service),
                         hashlib.sha256(canonical.encode("utf-8")).hexdigest()])

    k_date = _sign(("TC3" + skey).encode("utf-8"), date)
    k_svc = _sign(k_date, service)
    k_sign = _sign(k_svc, "tc3_request")
    sig = hmac.new(k_sign, to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    auth = ("%s Credential=%s/%s/%s/tc3_request, SignedHeaders=%s, Signature=%s"
            % (algo, sid, date, service, signed_headers, sig))

    req = urllib.request.Request("https://" + host, data=body, method="POST")
    for k, v in (("Authorization", auth), ("Content-Type", ct),
                 ("Host", host), ("X-TC-Action", action),
                 ("X-TC-Version", version), ("X-TC-Timestamp", str(ts)),
                 ("X-TC-Region", region)):
        req.add_header(k, v)

    try:
        raw = urllib.request.urlopen(req, timeout=60, context=CTX).read()
        return json.loads(raw.decode("utf-8", "ignore")).get("Response", {}), None
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", "ignore")[:300]
        return None, "HTTP %d %s" % (e.code, txt)
    except Exception as e:      # noqa: BLE001
        return None, repr(e)[:200]


def err_of(resp):
    if not resp:
        return "空响应"
    e = resp.get("Error")
    return "%s: %s" % (e.get("Code"), e.get("Message")) if e else None


# ---------------------------------------------------------------- 打包

def pick_src(f):
    """同名文件可能躺在好几处（工作区根目录、exe 交付目录）。

    GUI 运行时写的是 exe 旁边那份，通常才是用户最新改过的；
    一律取「最后修改的」，免得界面上改了行程、云端却还在跑旧配置。
    """
    cands = [p for p in (os.path.join(BASE, f),
                         os.path.join(BASE, "12306监控工具", f))
             if os.path.exists(p)]
    return max(cands, key=os.path.getmtime) if cands else None


def build_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in CODE_FILES:
            p = pick_src(f) or os.path.join(BASE, f)
            if not os.path.exists(p):
                raise SystemExit("缺少文件：%s" % f)
            if os.path.dirname(os.path.abspath(p)) != os.path.abspath(BASE):
                print("  %s ← 取用 GUI 目录里那份（更新时间更晚）" % f)
            z.write(p, f)
    raw = buf.getvalue()
    print("  代码包 %.1f KB" % (len(raw) / 1024))
    return base64.b64encode(raw).decode("ascii")


DEFAULT_REPO = "coke525/12306-monitor"


def _load_cred():
    """凭据可能在工作目录，也可能只在 exe 交付目录里，两处都找一遍"""
    out = {}
    for p in (os.path.join(BASE, "push_config.json"),
              os.path.join(BASE, "12306监控工具", "push_config.json")):
        if not os.path.exists(p):
            continue
        try:
            for k, v in json.load(io.open(p, encoding="utf-8")).items():
                if isinstance(v, str) and v.strip() and not out.get(k):
                    out[k] = v.strip()
        except Exception:
            pass
    return out


def _cfg():
    """读 monitor_config.json（同样两边都找，取最新的那份）"""
    for p in (pick_src("monitor_config.json") or os.path.join(BASE, "monitor_config.json"),):
        try:
            return json.load(io.open(p, encoding="utf-8")) or {}
        except Exception:      # noqa: BLE001
            return {}
    return {}


def env_of():
    c = _load_cred()
    spt = c.get("wxpusher_spt") or os.environ.get("WXPUSHER_SPT", "").strip()
    pat = c.get("github_pat") or os.environ.get("GITHUB_PAT", "").strip()
    repo = (c.get("github_repo") or os.environ.get("GITHUB_REPO", "").strip()
            or DEFAULT_REPO)
    if not (spt and pat):
        raise SystemExit("缺凭据：push_config.json 里要有 wxpusher_spt 和 github_pat")
    # 每日心跳：北京时间几点推一条「还活着」。0 = 不推
    try:
        hb = int(_cfg().get("heartbeat_hour", 8) or 0)
    except Exception:      # noqa: BLE001
        hb = 8
    return [{"Key": "WXPUSHER_SPT", "Value": spt},
            {"Key": "GITHUB_PAT", "Value": pat},
            {"Key": "GITHUB_REPO", "Value": repo},
            {"Key": "STATE_MODE", "Value": "github"},
            {"Key": "HEARTBEAT_HOUR", "Value": str(max(0, min(23, hb)))}]


def set_trigger(region, sid, skey, enable, func=FUNC_NAME, trig="every-minute"):
    """暂停 / 恢复云函数的定时触发器（不动函数本身，配置和代码都留着）。

    enable=True → OPEN；False → CLOSE。返回 (ok, 说明)
    注意：必须带 Type，否则报 MissingParameter。
    """
    r, err = call_api("UpdateTriggerStatus", region, sid, skey, {
        "FunctionName": func, "TriggerName": trig,
        "Qualifier": "$LATEST", "Type": "timer",
        "Enable": "OPEN" if enable else "CLOSE"})
    if err:
        return False, err
    e = err_of(r)
    return (False, e) if e else (True, "")


def trigger_on(region, sid, skey, func=FUNC_NAME, trig="every-minute"):
    """查触发器当前是开还是关。返回 (是否开着, 说明)"""
    r, err = call_api("GetFunction", region, sid, skey, {"FunctionName": func})
    if err:
        return None, err
    e = err_of(r)
    if e:
        return None, e
    for t in (r or {}).get("Triggers") or []:
        if t.get("TriggerName") == trig:
            return bool(t.get("Enable")), "定时 %s" % (
                (t.get("TriggerDesc") or "").replace("\\", ""))
    return None, "没找到触发器 %s" % trig


# ---------------------------------------------------------------- 主流程

def deploy(region, sid, skey, log=None):
    """完整部署一轮（打包 → 建/更新函数 → 触发器 → 验证）。

    log 用来接输出：命令行默认 print，GUI 传自己的 say 就能打到界面上。
    返回 0 表示成功。
    """
    say = log or print

    try:
        env = env_of()
    except SystemExit as e:
        say("      凭据不全：%s" % e)
        return 4

    say("[1/5] 打包代码")
    zipb64 = build_zip()

    say("[2/5] 创建云函数 %s（%s）" % (FUNC_NAME, region))
    payload = {
        "FunctionName": FUNC_NAME,
        "Code": {"ZipFile": zipb64},
        "Handler": HANDLER,
        "Runtime": RUNTIME,
        "Timeout": TIMEOUT,
        "MemorySize": MEMORY,
        "Environment": {"Variables": env_of()},
    }
    r, err = call_api("CreateFunction", region, sid, skey, payload)
    if err:
        say("      失败：%s" % err)
        return 3
    e = err_of(r)
    if e:
        if "AlreadyExist" in e or "已存在" in e:
            say("      函数已存在，改为更新代码与配置")
            r2, err2 = call_api("UpdateFunctionCode", region, sid, skey, {
                "FunctionName": FUNC_NAME, "ZipFile": zipb64,
                "Handler": HANDLER})
            if err2:
                say("      更新代码失败：%s" % err2)
                return 3
            r3, err3 = call_api("UpdateFunctionConfiguration", region, sid, skey, {
                "FunctionName": FUNC_NAME, "Timeout": TIMEOUT,
                "MemorySize": MEMORY,
                "Environment": {"Variables": env_of()}})
            if err3:
                say("      更新配置失败：%s" % err3)
                return 3
            say("      已更新")
        else:
            say("      失败：%s" % e)
            return 3
    else:
        say("      已创建")

    say("[3/5] 配置定时触发器（cron %s）" % CRON_7)
    r, err = call_api("CreateTrigger", region, sid, skey, {
        "FunctionName": FUNC_NAME, "TriggerName": "every-minute",
        "Type": "timer", "TriggerDesc": CRON_7})
    if err:
        say("      失败：%s" % err)
    else:
        e = err_of(r)
        # 已存在的触发器直接复用，每次重复部署都会撞到，不算失败
        if "已经存在" in str(e) or "AlreadyExist" in str(e):
            say("      触发器已存在，直接复用")
        else:
            say("      已创建" if not e else "失败：%s" % e)

    say("[4/5] 等待函数就绪")
    time.sleep(6)

    say("[5/5] 立刻调用一次验证")
    r, err = call_api("Invoke", region, sid, skey, {
        "FunctionName": FUNC_NAME, "InvocationType": "RequestResponse",
        "LogType": "Tail"})
    if err:
        say("      调用失败：%s" % err)
    else:
        e = err_of(r)
        if e:
            say("      调用返回错误：%s" % e)
        tail = r.get("Log") or ""
        if log:
            say("      ---- 云端日志 ----")
            for line in tail.splitlines()[:30]:
                say("      " + line)
        res = r.get("Result") or {}
        say("      返回码：RetMsg=%s" % (res.get("RetMsg") or res.get("FunctionRequestId") or ""))

    say("\n完成：%s 已在 %s 每 1 分钟跑一次。" % (FUNC_NAME, region))
    say("控制台：https://console.cloud.tencent.com/scf/list")
    return 0


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    sid, skey = sys.argv[1].strip(), sys.argv[2].strip()
    region = sys.argv[3].strip() if len(sys.argv) > 3 else "ap-guangzhou"
    return deploy(region, sid, skey)


if __name__ == "__main__":
    sys.exit(main())
