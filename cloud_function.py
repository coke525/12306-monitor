#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
云函数通用入口（腾讯云 SCF / 阿里云 FC / 华为云 FG 等都适用）
==============================================================
各平台只是入口函数名不同，跑的都是同一段 run_once()。

  腾讯云 云函数 SCF      main_handler(event, context)
  阿里云 函数计算 FC     handler(event, context)
  华为云 FunctionGraph   handler(event, context)
  百度云 CFC             handler(event, context)

部署时把这几个文件一起打包上传：
  cloud_function.py  本文件（入口）
  cloud_monitor.py   扫描 + 对比 + 推送
  train_monitor.py   12306 查询与方案拼装
  push.py            推送通道
  monitor_config.json  你的行程配置

必需环境变量：
  WXPUSHER_SPT      微信推送 token（SPT_ 开头）
  GITHUB_PAT        读写状态快照用的令牌（可用 GitHub 的 PAT）
  GITHUB_REPO       仓库，如 coke525/12306-monitor

说明：云函数是无状态的，容器回收后 /tmp 就没了，
所以状态快照存在 GitHub 仓库里（cloud_state.json），靠 GITHUB_PAT 读写。
不想存远端也行：把 STATE_MODE 设为 file 并把 STATE_FILE 指向有持久化的挂载路径，
但那样每次冷启动都当"首次运行"，只会建基线、不会推送 —— 不推荐。

定时器：在云平台控制台配「定时触发器」。注意各家的 cron 位数不一样：
  腾讯云 SCF    7 位（秒 分 时 日 月 星期 年）  ->  0 */1 * * * * *
  阿里云 FC     6 位（秒 分 时 日 月 星期）     ->  0 */1 * * * *
  自己的服务器   5 位（分 时 日 月 周）          ->  * * * * *
详见仓库里的「部署指南.md」。
"""

import os
import sys

BASE = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
        else os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import cloud_monitor as cm   # noqa: E402


def run_once():
    """各平台统一调用的实际逻辑"""
    cm.run_once()
    return {"ok": True}


# 腾讯云 SCF
def main_handler(event, context):
    return run_once()


# 阿里云 FC / 华为云 FG / 百度云 CFC
def handler(event, context):
    return run_once()


# 部分平台用 index / lambda 命名
def index(event, context):
    return run_once()


def lambda_handler(event, context):
    return run_once()


if __name__ == "__main__":
    print(run_once())
