# 12306 余票监控 · 云端版

已经跑在 GitHub Actions 上：**每 5 分钟触发一次，任务内循环 3 轮，实际约 77 秒扫一次**，
电脑关机也照扫，有新票直接推微信。

> **想换到腾讯云函数 / 阿里云函数计算 / 自己的服务器？** 看 👉 [`部署指南.md`](部署指南.md)
> 监控逻辑本身不绑定平台，换平台只是换"谁定时调用它"。
> exe 里「部署平台」下拉选好点「生成部署包」，会自动整理好要上传的文件。

---

## 仓库里每个文件是干嘛的

```
.github/workflows/ticket.yml   定时任务本体（GitHub Actions 用）
cloud_monitor.py               云端入口：读环境变量凭据 + 跨次状态记忆
cloud_function.py              云函数入口（腾讯云/阿里云/华为云通用）
train_monitor.py               扫描 12306 + 拼换乘方案 + 判定是否符合你的窗口
push.py                        微信推送（被 cloud_monitor 引用，必须传）
monitor_config.json            ★ 唯一的配置文件，改行程只动它
monitor_gui.py                 桌面配置工具的源码（12306Monitor.exe 就是它打包的）
12306Monitor.exe               桌面配置工具本体（Windows，双击运行）
使用说明.txt                    上面那个 exe 的操作说明
部署指南.md                     四种部署平台的详细操作步骤
cloud_state.json               上轮余票快照，由脚本自动提交，别手改
```

### 怎么拿到 exe 到本地用

在仓库文件列表里点 `12306Monitor.exe` → 右上角 **Download raw file**（或 `···` → Download）。

**下载后必须和 `monitor_config.json` 放在同一个文件夹里**，单独运行会读不到配置。
最简单的做法：新建一个空文件夹，把这两个文件都丢进去，双击 exe 即可。

改行程 / 改时段**只用动 `monitor_config.json`**。更省事的做法是用
`12306Monitor.exe`：界面里填完 →「保存配置」→「同步到 GitHub」。

---

## 改行程只要动一个文件：`monitor_config.json`

提交后下一轮扫描自动生效，不用碰代码。

```jsonc
{
  "dates": ["2026-10-06", "2026-10-07"],        // 要盯哪几天

  "date_prefs": {                                // 每天的时间窗口
    "2026-10-06": {
      "dep_from": "09:00",                       // 最早几点出发
      "dep_to":   "17:30",                       // 最晚几点出发
      "arr_zz_by": "18:10",                      // 首段到换乘城市不晚于（可 null）
      "final_by": "20:00"                        // 最终到终点不晚于 ★最关键
    }
  },

  "origins": ["PDF", "BFF", "PEN"],              // 可接受的上车站
  "dest": "JIF",                                 // 终点
  "transfer_cities": ["ZZF", "ZAF", "ZIF"],      // 允许在哪换乘
  "via": "JOF",                                  // 二次换乘点（只在郑州换一次就填终点）
  "fast_mid": ["ZZF", "ZAF"],                    // fast 模式实际扫的换乘站

  "scan_mode": "fast",                           // fast≈40秒/轮；full≈90秒（更全）
  "only_seated": false                           // true = 只推全程有座的方案
}
```

路线模型固定为 `origins → transfer_cities → via → dest`（1~3 段）。
直达就把 `via` 填成 `dest`；只要一次换乘也是 `via = dest`。

### 车站电报码

`stations` 里已经内置**全国 3404 个车站**的电报码，直接填三字母代码即可，不用自己加。
常用：平顶山 `PEN`、平顶山西 `PDF`、宝丰 `BFF`、南阳 `NFF`、郑州 `ZZF`、
郑州东 `ZAF`、郑州航空港 `ZIF`、焦作 `JOF`、焦作西 `JIF`、南阳寨 `NYF`、
北京 `BJP`、上海 `SHH`。查别的站用 exe 里的「查车站代码」框。

### 三个容易踩的坑

1. **`dates` 和 `date_prefs` 必须成对**。只加日期不加窗口 → 那天一条都不推，还不报错。
2. **换 `transfer_cities` 要同步改 `fast_mid`**，否则 fast 模式下新换乘站会被裁掉、扫不到票。
3. 新车站不用补 `stations`（已全量内置）。

**例：改成"10-08 从平顶山西出发，18:00 前到焦作西"**
→ `dates` 改成 `["2026-10-08"]`，`date_prefs` 里加
`"2026-10-08": {"dep_from":"08:00","dep_to":"14:00","arr_zz_by":null,"final_by":"18:00"}` → 提交。

---

## 重新部署到新仓库时（可选）

1. 建**私有**仓库，上传 `cloud_monitor.py` / `train_monitor.py` / `push.py` /
   `monitor_config.json` / `.github/workflows/ticket.yml`
2. Settings → Secrets and actions → Actions → New repository secret：
   `WXPUSHER_SPT` = `SPT_ 开头的极简推送 token`
   （可选 `PUSHPLUS_TOKEN` / `WXPUSHER_TOKEN` / `WXPUSHER_UID`）
3. Settings → Actions → General → Workflow permissions → **Read and write**
   （不开这条，脚本回写 `cloud_state.json` 会 403）
4. Actions 页 → 选 `12306 余票监控` → Run workflow 验一次

`cron` 用 **UTC**：现在的两条覆盖**北京时间 07:00–23:55** 每 5 分钟。
GitHub 对 60 天无活动的仓库会停用定时任务，长期跑要偶尔手动触发一次。

### 扫描频率与额度

GitHub 定时最小粒度 5 分钟，所以任务内循环 3 轮、间隔 15 秒 → 实测约 77 秒一轮。
**免费私有仓库 2000 分钟/月，这个频率约 1000 分钟/天，撑约 2 天**。
要长期跑：把 workflow 里的 `for i in 1 2 3` 改成 `for i in 1`（回到 5 分钟粒度，省 3 倍），
或换公开仓库（额度不限），或上云函数。

---

## 想要 1 分钟粒度：换云函数

境内 IP、不会被限流、免费额度够用，抢票比 GitHub 准。

1. 腾讯云控制台 → 云函数 SCF → 新建 → 运行环境 **Python 3.10**
2. `cloud_monitor.py` + `train_monitor.py` + `push.py` + `monitor_config.json` 打 zip 上传，
   入口 `cloud_monitor.main`
3. 环境变量填 `WXPUSHER_SPT`、`STATE_FILE=/tmp/state.json`
4. 定时触发器 Cron：`0 */1 7-23 * * *`（北京时间 7–23 点每分钟）

安卓还可以用 Termux：`pkg install python git` → 拉仓库 →
`while true; do python cloud_monitor.py; sleep 60; done`，配合 `termux-wake-lock`。

---

## 推送通道

- 主力：**WxPusher 极简推送**（`SPT_` 开头），不需实名，已验证能收到
- PushPlus 因未实名发不出去，当备份
- 极简推送走 GET，内容硬限制约 160 字，脚本会自动截断成精简版；
  完整方案列表用 exe 的「立即扫描」在本机看
