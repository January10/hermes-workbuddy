# workbuddy — Hermes 的 WorkBuddy 模型提供方（进程内，无服务）

把本机 **两类 WorkBuddy 桌面登录**接进 Hermes，和 DSH 的 `dsh-workbuddy-connect` 一一对应：
它检测本机的国内版/国际版账户并注册两个提供方，这里也一样。

| 提供方 | 桌面登录文件 | 上游 | 账号形态 |
| --- | --- | --- | --- |
| `workbuddy` | `workbuddy-desktop.info` | `copilot.tencent.com`（计费 `www.codebuddy.cn`） | 国内版（企业额度） |
| `workbuddy-ai` | `workbuddy-desktop-ai.info` | `www.workbuddy.ai` | 国际版（积分资源包） |

**没有任何常驻服务/端口/自启**：代码跑在 Hermes 进程里，凭据只读桌面登录、从不写入桌面状态。
配套（可选）：`plugins/workbuddy-models/` 把两个变体的模型表、**积分倍率**、额度做成
会话内 `/workbuddy` 命令与 `hermes workbuddy-models` CLI。

## 装了什么

```
%LOCALAPPDATA%\hermes\plugins\model-providers\workbuddy\
├── __init__.py      两个变体的 profile（register_provider ×2，共用一套钩子）
├── plugin.yaml      kind: model-provider
└── README.md        本文件
```

对照关系（DSH 插件 → 本插件）：

| DSH `dsh-workbuddy-connect` | Hermes 插件 |
| --- | --- |
| `inject=['llm']` 注册两个变体 | `_build_profile(CN/GLOBAL, ...)` + `register_provider` ×2 |
| 凭据存储（读两个 `.info`、`.logged-out`、过期判断、自己的副本） | `_desktop_record(variant)` 现读 + 凭据池行 + `refresh_credential`（桌面记录优先，过期才打上游 refresh） |
| 内部 shim（强制 stream、`developer→system`、首条 system、max_tokens 下限） | `prepare_messages()` / `build_api_kwargs_extras()`（进程内，每请求执行） |
| `GET /v3/config` + cli 名单 ∩ 可用行 | `fetch_models()`（`data.agents[name=cli].models` ∩ in/out > 0 的行） |
| 卡片里的倍率 / 推理档位 | `model_credits` / `model_efforts`（抓目录时学回来）→ `/workbuddy` 面板、`/usage` |
| 卡片里的额度（国际版资源包 / 国内版企业额度） | `fetch_account_usage()`（国际版 `get-user-resource`；国内版 `get-enterprise-user-usage`，`limitNum: -1` = 不限量） |

## 用之前

一次性把两个登录写进凭据池（插件只读桌面登录，不动它）：

```bash
hermes auth add workbuddy        # 国内版 → 凭据池 source=manual:desktop-login
hermes auth add workbuddy-ai     # 国际版
hermes auth status workbuddy     # 看状态（两个同形）
hermes auth refresh workbuddy-ai # 手动轮换（401 时 Hermes 也会自动调）
```

凭据池里的 access token 以明文存在 `%LOCALAPPDATA%\hermes\auth.json`（与 Hermes 其它自带提供方一致）。
行来源必须以 `manual:` 开头，否则 Hermes 会在落盘时把 secret 抹掉（`agent/credential_persistence.py`
的"借用凭据"规则）。**换变体/换账号后**记得 `auth logout <id>` 再 `auth add <id>`，否则池里留着旧区域的令牌。

打开模型选择器（`/model`、`hermes model`、桌面 App）时 `fetch_models()` 也会自动补种子。

**必须**在 `config.yaml` 里声明这两个端点是流式专用，否则压缩/标题这类辅助调用会被上游 `400/11101` 拒：

```yaml
auxiliary:
  stream_only_base_urls:
    - workbuddy.ai
    - copilot.tencent.com
    - codebuddy.cn
```

## 用法

```bash
hermes chat -q "你好" --provider workbuddy-ai -m deepseek-v4.1-flash   # 国际版（免费档）
hermes chat -q "你好" --provider workbuddy    -m hy3                   # 国内版（免费档）
hermes model                                                           # 选择器里两组：WorkBuddy / WorkBuddy AI
hermes usage --provider workbuddy-ai                                   # 额度（含"模型倍率"一行）
hermes workbuddy-models [cn|ai|models|usage] [--json]                  # 全量清单（倍率/上下文/能力/额度）
```

会话内：`/model` 切换、`/usage` 看额度、`/workbuddy` 看两个变体的模型倍率表。

## 代价与边界

- 每次对话消耗对应账号的额度：**国际版**扣积分（`×0` = 免费档，如 `deepseek-v4.1-flash`/`hy3`）；
  **国内版**当前是企业账号 `limitNum: -1`（不限量），具体仍以企业策略为准。
- 倍率、上下文、推理档位全部来自账号目录的实时字段（`credits` / `contextWindow.defaultLength` /
  `reasoning.supportedEfforts`），不写死。
- 两个变体的 UA 形状不同：国内版用 CLI 身份（`CLI/2.63.2 CodeBuddy/2.63.2`），国际版用桌面 App 身份
  （目录另试 `WorkBuddyAI/<版本>`）——这是上游按客户端分流目录的口径，别统一。
- 上游是客户端接口而非官方 API；WorkBuddy 客户端更新后可能需要同步 `headers`/`UA`/路径。
- 桌面端退出登录后对应提供方会报 401，并提示重新登录。
- 移除：删掉本目录（和 `plugins/workbuddy-models/`）+ `config.yaml` 里那三条 `stream_only_base_urls`。
