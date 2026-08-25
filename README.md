# Stella 整点联合报告

每小时自动运行的行情联合报告。**仅触发告警时推送**（节省推送额度），无异常时静默只记录日志：

- 告警条件：个股当日涨跌 **±3%**，或组合当日加权盈亏 **±1.5%**
- 推送渠道：PushPlus 微信推送，一条消息合并完整版报告（标题带 ⚠️ 前缀）

报告内容包含：

- 美债收益率（10Y / 2Y / 利差）
- 美股三大指数（标普 / 道指 / 纳指）
- 美元指数 · 黄金 · WTI 原油
- 13 只持仓最新价与当日涨跌（±3% 异动 ⚠️ 标注）
- 组合加权日盈亏（±1.5% ⚠️ 标注）及贡献前 3

不含（无免费数据接口，由本地 WorkBuddy 会话补充）：FOMC 概率、个股新闻解读、财报与经济数据日程、霍尔木兹海峡通船数。

## 部署步骤

1. 将本目录推送到 GitHub（建议私有仓库）。
2. 仓库 Settings → Secrets and variables → Actions → New repository secret：
   - Name：`PUSHPLUS_TOKEN`
   - Value：PushPlus Token（[www.pushplus.plus](https://www.pushplus.plus) 获取）
3. Actions 页确认 `hourly-report` 工作流已启用；可点 Run workflow 手动触发一次测试。
4. 之后每小时整点自动运行（北京时间 8:00 至次日 1:59，凌晨 2:00-7:59 静默，cron `0 0-17 * * *`，GitHub 调度可能延迟几分钟）；**仅触发告警时推送**，无异常不推送。

## 本地测试

```bash
export PUSHPLUS_TOKEN=xxxx           # Windows PowerShell: $env:PUSHPLUS_TOKEN="xxxx"
python hourly_report.py --dry       # 只打印不推送
python hourly_report.py             # 打印并推送
```

## 说明

- 仅用 Python 标准库，无第三方依赖；单次运行约 1 分钟。
- 行情主源 CNBC（批量请求），备源 Yahoo Finance（CNBC 缺数据时逐个回退）。
- 数据仅供参考，不构成投资建议。
