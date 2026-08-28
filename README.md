# Stella 行情报告（云端简易版）

四个任务槽位自动运行的行情数据报告，**每次都推送**，由 GitHub Actions 云端运行，**不依赖本地电脑开机**，确保微信必达。本地另有福福深度版同时段并行（六维分析、新闻解读等）。

## 推送排期

| 时段 | 北京时间 | UTC cron | 说明 |
|---|---|---|---|
| 晨报 | 09:00 | `0 1 * * *` | 全年；避开 0:00 UTC 拥堵时段 |
| 霍尔木兹简易版 | 16:00 | `0 8 * * *` | 全年；WTI/布伦特/美元/黄金快照 |
| 盘前（夏令时） | 20:30 | `30 12 * * *` | 美股 21:30 开盘前 1 小时 |
| 盘前（冬令时） | 21:30 | `30 13 * * *` | 美股 22:30 开盘前 1 小时 |

盘前两个触发点同时挂载，脚本内按美国夏令时规则（3 月第二个周日 ～ 11 月第一个周日）自动取舍，错季的触发点静默跳过，无需人工切换。

## 推送策略

每个槽位一条消息：

**每次都推（常规数据）：**

- 美债收益率：1Y / 2Y / 5Y / 10Y / 30Y（含 10Y-2Y 利差）
- 美元指数 · 黄金 COMEX · WTI 原油

**仅触发异动时展示（异动部分）：**

- 个股 ±3%（仅展示触发阈值的持仓，无异动则整段跳过）
- 组合当日加权 ±1.5%（触发时展示，含贡献前 3）
- 三大指数 ±1.5%（触发时展示）

### 阈值一览

| 项目 | 阈值 |
|---|---|
| 个股当日涨跌 | ±3% |
| 组合当日加权 | ±1.5% |
| 三大指数 | ±1.5% |
| 美债收益率日变动 | ±8bp（仅标注 ⚠️） |
| 美元指数 | ±1%（仅标注 ⚠️） |
| 黄金 | ±1.5%（仅标注 ⚠️） |
| WTI 原油 | ±3%（仅标注 ⚠️） |

> ⚠️ 标注仅作视觉提示，不影响推送逻辑——收益率和美元/黄金/原油板块每次都推。

## 推送渠道（双通道免费额度调度）

两个都是免费账户，脚本自动调度：

1. **PushPlus**（主）：`https://www.pushplus.plus/send`，JSON POST，template=markdown，成功码 `code==200`
2. **方糖 Server酱**（备）：`https://sctapi.ftqq.com/<SENDKEY>.send`，成功码 `code==0`

先走 PushPlus；额度用尽或请求失败时自动回退方糖，两者都失败才判定推送失败（exit 1）。

Token 均走环境变量 / GitHub Secrets，**禁止写入代码**：

- `PUSHPLUS_TOKEN` — PushPlus Token
- `SERVERCHAN_KEY` — 方糖 SendKey（`SCT` 开头）

## 持仓维护

`hourly_report.py` 顶部 `HOLDINGS` 列表维护持仓（ticker、权重%、平均成本）。
Stella 每天上午发持仓截图 → 福福更新本文件并推送 GitHub → 云端自动生效。
不发截图则沿用昨日持仓。

## 部署

仓库：`ProfessionalPlayeronEarth/Stock-summary-and-alert`

1. Settings → Secrets and variables → Actions → New repository secret：
   - `PUSHPLUS_TOKEN`
   - `SERVERCHAN_KEY`
2. Actions 页确认 `stella-report` 工作流（`.github/workflows/stella-report-v2.yml`）已启用；可点 Run workflow 手动触发测试（可选 `slot`：morning / premarket / hormuz）。
3. 之后每日自动运行（见上方推送排期表）。

> 注：2026-08-28 曾遇到 GitHub schedule 触发器静默失联（手动 dispatch 正常、定时从不触发，社区已知问题），已通过**换新 workflow 文件名重建**强制重新注册 schedule（现为 v2 文件名）。若再次失联，可走 `repository_dispatch` 兜底：向 `POST /repos/<owner>/<repo>/dispatches` 发送 `{"event_type":"run-report"}`（带 PAT 头）即可触发，可用 cron-job.org 等外部免费 cron 定时调用。

## 本地测试

```bash
export PUSHPLUS_TOKEN=xxxx           # Windows PowerShell: $env:PUSHPLUS_TOKEN="xxxx"
export SERVERCHAN_KEY=SCTxxxx        # Windows PowerShell: $env:SERVERCHAN_KEY="SCTxxxx"
python hourly_report.py --dry       # 只打印不推送
python hourly_report.py             # 打印并推送
SLOT=hormuz python hourly_report.py # 强制指定槽位测试
```

## 说明

- 仅用 Python 标准库，无第三方依赖；单次运行约 1 分钟。
- 行情主源 CNBC（批量请求），备源 Yahoo Finance（CNBC 缺数据时逐个回退）。
- 数据仅供参考，不构成投资建议。
