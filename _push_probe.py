# -*- coding: utf-8 -*-
"""一次性验证脚本：确认打包成 exe 后 push.py 能定位到 exe 旁边的 push_config.json。

跑法：把本脚本打成 console exe，放进任意目录（该目录内放一份 push_config.json），
直接运行，看 frozen / BASE_DIR / CFG / 结果 四行输出。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import push  # noqa: E402

print("frozen   =", getattr(sys, "frozen", False))
print("exe      =", sys.executable)
print("BASE_DIR =", push.BASE_DIR)
print("CFG      =", push.CFG)
print("CFG存在  =", os.path.exists(push.CFG))
cfg = push.load_cfg()
print("读到字段 =", sorted(k for k, v in cfg.items() if str(v).strip()))
ok, msg = push.send("12306 打包自检",
                    "这条来自打包后的 exe，收到说明 exe 内的推送链路是通的。",
                    short="打包自检：exe 内推送链路正常")
print("推送结果 =", ok, "|", msg)
