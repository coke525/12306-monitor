# 关机也能收推送：把监控搬到云上

现在这套是本机 Python + 本机定时任务，**电脑一关机就断**。要 7×24 跑，必须有
一台永远在线的机器替你扫票。下面按"从省事到靠谱"排了三条路。

---

## 方案对比

| 方案 | 成本 | 扫描频率 | 出口 IP | 靠谱度 | 备注 |
|---|---|---|---|---|---|
| **A. GitHub Actions** | 免费 | 5 分钟（实际可能延迟几分钟） | 境外 | 中 | 不用实名、不用服务器，5 分钟配完。缺点是境外 IP 可能被 12306 限流 |
| **B. 腾讯云/阿里云 云函数** | 免费额度内 | **1 分钟** | **境内** | 高 | 最准，抢票强推荐；需要实名认证过的云账号 |
| **C. 手机 Termux（安卓）** | 0 元 | 1 分钟 | 境内 | 高 | 手机不关机就行，等于白捡一台服务器 |

> 抢 K258 这种"放票 6 分钟被抢光"的车，A 的 5 分钟粒度 + 延迟有风险。
> 建议先花 5 分钟搭 A，跑一次看日志；如果发现查不到票（境外 IP 被拦），再上 B。

---

## 方案 A：GitHub Actions（推荐先试）

### 1. 建一个**私有**仓库
GitHub 新建仓库，名字随便（如 `12306-monitor`），**必须选 Private**（里面有你的推送逻辑）。

### 2. 上传 4 个文件
仓库根目录放这些：

```
cloud_monitor.py
train_monitor.py
.github/workflows/ticket.yml
```

（`push.py`、`push_config.json` 不用传 —— 云端走环境变量，token 不进代码）

### 3. 填 Secrets（关键）
仓库 → **Settings → Secrets and variables → Actions → New repository secret**，加一条：

| Name | Value |
|---|---|
| `WXPUSHER_SPT` | `SPT_8Uyh2DAzK9WWrpqqU8S7BNnadV07` |

可选再加 `PUSHPLUS_TOKEN`、`WXPUSHER_TOKEN`、`WXPUSHER_UID`，不加也能跑。

### 3.5 打开写入权限（不做这一步，最后一次 commit 会 403）
仓库 → **Settings → Actions → General → Workflow permissions**
→ 选 **Read and write permissions** → Save。
（脚本要靠把 `cloud_state.json` 提交回仓库来记住上次余票，必须有写权限）

### 4. 手动跑一次验证
仓库 → **Actions** → 左侧选 `12306 余票监控` → 右上角 **Run workflow**。

看日志：
- 出现 `[cloud] 首次运行，只建立基线快照，不推送` → 正常，第一次只存基线
- 之后再跑一次，如果有命中偏好的新方案，就会往微信推
- 如果全是 `WARN ... 查询失败` 或 `有票 0 趟` → **境外 IP 被 12306 拦了**，换方案 B

### 5. 保持定时不失效
GitHub 对**60 天无活动**的仓库会自动停用定时任务。10-06 之前每天都会跑，不会触发。

### 时区说明
`cron` 用 UTC。workflow 里已经写好两条，覆盖**北京时间 07:00–23:55** 每 5 分钟。
想改成全天，把 schedule 换成一行 `cron: "*/5 * * * *"`。

### 状态怎么记住的
云端容器每次都是全新的，靠把 `cloud_state.json` 提交回仓库来记住"上一次的余票"。
workflow 最后一步已经写好自动 commit + push。

---

## 方案 B：腾讯云函数（更准，1 分钟粒度）

1. 腾讯云控制台 → **云函数 SCF** → 新建 → 模板选"从头开始"，运行环境 **Python 3.10**
2. 函数代码用 `cloud_monitor.py` + `train_monitor.py` 打个 zip 上传，
   **入口改成 `cloud_monitor.main`**（或把 main 包进 `index.main_handler`）
3. 环境变量填 `WXPUSHER_SPT`
4. 触发方式：**定时触发器**，Cron 写 `0 */1 7-23 * * *`（北京时间每天 7–23 点每分钟）
5. 状态持久化：把 `STATE_FILE` 指向 `/tmp/state.json`（实例复用期内有效），
   或接 COS / 云数据库的简单 KV。实例被回收时会丢基线，最多重复推一次，可接受。

---

## 方案 C：手机 Termux（零成本，境内 IP）

安卓装 Termux（F-Droid 版），然后：

```bash
pkg install python git
git clone <你的私有仓库> monitor
cd monitor
export WXPUSHER_SPT=SPT_xxx
# 每 60 秒跑一次
while true; do python cloud_monitor.py; sleep 60; done
```

手机锁屏后 Termux 可能被杀后台 → 在 Termux 里执行
`termux-wake-lock`，并在系统设置里把 Termux 设为**电池优化白名单**。

---

## 推送通道现状

- 主力：**WxPusher 极简推送**（`SPT_` 开头），不需要实名，已验证能收到
- PushPlus 那个 token 因为没实名，发不出去，暂时当备份
- 极简推送走 GET，内容有硬长度限制（约 160 字），脚本里已自动截断成精简版，
  完整方案细节看本地看板 http://127.0.0.1:8765
