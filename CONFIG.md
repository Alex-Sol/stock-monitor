# 股票监控系统配置文档

> 更新日期：2026-07-18
> 配置文件：`backend/config.json`（已加入 .gitignore，不提交到 GitHub）

---

## 快速参考

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| LLM 开关 | `false` | 默认走**规则模式**，设为 `true` 启用大模型分析 |
| 通知方式 | 企业微信机器人 Webhook | 唯一生效的外部通知渠道 |

---

## 一、通知渠道配置

### 企业微信机器人 Webhook（当前唯一在用）

```json
{
  "webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=YOUR_KEY"
}
```

| 字段 | 值 | 说明 |
|------|-----|------|
| `webhook_url` | `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=62d2f813-d129-4db6-84cc-31f197a98030` | 企业微信群机器人地址 |

获取方式：在企业微信群 → 群设置 → 添加群机器人 → 复制 Webhook 地址。

**注意：** 当前系统仅支持此通知方式。企业微信应用模式（需要 corp_id/agent_id/secret）因 IP 白名单限制已弃用。

---

## 二、LLM 大模型配置

```json
{
  "llm": {
    "enabled": false,
    "api_key": "",
    "base_url": "https://api.kimi.com/coding/v1",
    "model": "kimi-k2.6",
    "timeout": 60,
    "temperature": 0.6
  }
}
```

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `enabled` | `false` | **主开关**。`false`=规则模式，`true`=LLM 模式 |
| `api_key` | `""` | Kimi API Key。格式：`sk-kimi-...` |
| `base_url` | `https://api.kimi.com/coding/v1` | Kimi OpenAI 兼容端点 |
| `model` | `kimi-k2.6` | 模型名称。当前支持 `kimi-k2.6` |
| `timeout` | `60` | 请求超时（秒） |
| `temperature` | `0.6` | 采样温度。Kimi K2.6 固定为 0.6 |

### 两套分析机制说明

| 模式 | 触发条件 | 选股 | 预警 | 日报 |
|------|---------|------|------|------|
| **规则模式**（默认） | `llm.enabled: false` | 代码逻辑过滤 + 评分排序 | 涨跌幅/成交量阈值触发 | 模板拼接 + 规则判断情绪 |
| **LLM 模式** | `llm.enabled: true` | 大模型分析全市场数据后推荐 | 大模型识别异常模式 | 大模型生成自然语言总结 |

**建议：**
- 日常运行建议规则模式（稳定、快速、零成本）
- 需要深度分析时切换 LLM 模式（消耗 API token）

---

## 三、选股参数配置

```json
{
  "thresholds": {
    "change_pct": 5.0,
    "volume_surge_ratio": 3.0,
    "rsi_lower": 40,
    "rsi_upper": 70,
    "pe_lower": 0,
    "pe_upper": 100,
    "min_volume_5d": 100000000,
    "max_change_20d": 0.5,
    "max_alerts_per_stock_per_day": 2
  }
}
```

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `change_pct` | `5.0` | 涨跌幅预警阈值（%）。≥此值触发预警 |
| `volume_surge_ratio` | `3.0` | 成交量异动倍数。今日成交量 > 前5日均量 × 此值 |
| `rsi_lower` | `40` | RSI 下限。低于此值不入选 |
| `rsi_upper` | `70` | RSI 上限。高于此值不入选 |
| `pe_lower` | `0` | PE 下限。PE ≤ 0 排除 |
| `pe_upper` | `100` | PE 上限。PE > 100 排除 |
| `min_volume_5d` | `100000000` | 5日平均成交额下限（元）。默认 1 亿 |
| `max_change_20d` | `0.5` | 20日最大涨幅。已涨超 50% 排除 |
| `max_alerts_per_stock_per_day` | `2` | 每只股票每日最多预警次数 |

---

## 四、自选股配置

```json
{
  "watchlist": ["600519", "300750", "000001", "002594", "600036"]
}
```

当前监控的自选股：

| 代码 | 名称 |
|------|------|
| 600519 | 贵州茅台 |
| 300750 | 宁德时代 |
| 000001 | 平安银行 |
| 002594 | 比亚迪 |
| 600036 | 招商银行 |

---

## 五、定时任务配置

```json
{
  "schedule": {
    "select_time": "09:25",
    "report_time": "15:05",
    "monitor_interval_minutes": 3
  }
}
```

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `select_time` | `"09:25"` | 开盘前选股时间 |
| `report_time` | `"15:05"` | 收盘后日报时间 |
| `monitor_interval_minutes` | `3` | 盘中监控轮询间隔（分钟） |

---

## 六、数据输出配置

```json
{
  "data_output_dir": "../data/"
}
```

| 字段 | 默认值 | 说明 |
|------|--------|------|
| `data_output_dir` | `"../data/"` | 数据文件输出目录。相对 backend/ 的父目录 |

输出文件：
- `candidates.json` — 选股结果
- `daily_report.json` — 结构化日报
- `report_YYYY-MM-DD.md` — Markdown 日报
- `market_summary.json` — 大盘概况
- `alerts.json` — 预警记录
- `watchlist.json` — 自选股数据

---

## 七、密钥汇总

| 密钥 | 用途 | 所在文件 |
|------|------|----------|
| `62d2f813-d129-4db6-84cc-31f197a98030` | 企业微信机器人 Webhook | `config.json → webhook_url` |
| `sk-kimi-3iTjjh2PuYn1PWVISJiEh0KVms2IvmcrW4aqC3uo6Zk8T15vHDx7W0ef3R8OBOd1` | Kimi LLM API | `config.json → llm.api_key` |

**安全提醒：**
- `config.json` 已加入 `.gitignore`，不会提交到 GitHub
- 修改密钥后直接编辑 `backend/config.json` 即可
- 不要将含真实密钥的文件手动 push 到远程

---

## 八、配置示例（完整）

```json
{
  "watchlist": ["600519", "300750", "000001", "002594", "600036"],
  "thresholds": {
    "change_pct": 5.0,
    "volume_surge_ratio": 3.0,
    "rsi_lower": 40,
    "rsi_upper": 70,
    "pe_lower": 0,
    "pe_upper": 100,
    "min_volume_5d": 100000000,
    "max_change_20d": 0.5,
    "max_alerts_per_stock_per_day": 2
  },
  "webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=62d2f813-d129-4db6-84cc-31f197a98030",
  "data_output_dir": "../data/",
  "llm": {
    "enabled": false,
    "api_key": "sk-kimi-3iTjjh2PuYn1PWVISJiEh0KVms2IvmcrW4aqC3uo6Zk8T15vHDx7W0ef3R8OBOd1",
    "base_url": "https://api.kimi.com/coding/v1",
    "model": "kimi-k2.6",
    "timeout": 60,
    "temperature": 0.6
  },
  "schedule": {
    "select_time": "09:25",
    "report_time": "15:05",
    "monitor_interval_minutes": 3
  }
}
```

---

## 九、LLM 与规则模式详解

系统同时支持两套分析机制，通过 `llm.enabled` 切换。

### 运行机制

| 模式 | 触发条件 | 选股 | 预警 | 日报 |
|------|---------|------|------|------|
| **规则模式**（默认） | `llm.enabled: false` | 代码逻辑过滤 + 评分排序 | 涨跌幅/成交量阈值触发 | 模板拼接 + 规则判断情绪 |
| **LLM 模式** | `llm.enabled: true` | 大模型分析全市场数据后推荐 | 大模型识别异常模式 | 大模型生成自然语言总结 |

### LLM 失败时的回退机制

三套模式在 LLM 调用失败（网络超时、API 错误等）时，均自动回退到规则模式：

| 模式 | LLM 输出 | LLM 失败时的回退输出 |
|------|---------|---------------------|
| **选股** | LLM 分析全市场数据，生成 Top 20 + 推荐理由 | `screen_stocks()` 规则筛选（RSI/PE/成交量等阈值过滤 + 评分排序） |
| **预警** | LLM 识别自选股异常模式，给出原因 | `AlertEngine` 规则引擎（涨跌幅 ≥ 阈值 / 成交量 > 前5日均量 × 倍数） |
| **日报** | LLM 生成完整 Markdown 日报 + 结构化数据 | `generate_daily_report()` 规则模板拼接（涨跌比 → Bullish/Neutral/Cautious） |

### 数据获取部分（始终规则）

无论 LLM 开关如何，以下数据始终由 `data_fetcher.py` 从新浪/东方财富获取，**不由 LLM 生成**：

- 大盘指数点位、涨跌幅、涨跌家数
- 个股行情（收盘价、成交量、成交额）
- 技术指标（RSI-14、MACD、MA20 等）
- 板块资金流向、行业排名

### 输出文件归属

| 输出文件 | LLM 模式内容 | 规则模式内容 |
|---------|-------------|-------------|
| `candidates.json` | LLM 选股 + 推荐理由 | 规则筛选 + 固定模板理由 |
| `alerts.json` | LLM 识别异动 + 原因 | 阈值触发 + 数值型理由 |
| `report_YYYY-MM-DD.md` | LLM 自然语言全文 | 规则模板拼接全文 |
| `daily_report.json` | LLM 结构化数据 | 规则结构化数据 |

### 使用建议

- **日常运行**：规则模式（稳定、快速、零 API 成本）
- **深度分析**：LLM 模式（消耗 token，但分析更自然、更结构化）
- **切换方式**：直接修改 `backend/config.json` 中 `llm.enabled` 字段，无需重启
