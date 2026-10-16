# hermes-workbuddy

WorkBuddy 模型在 [Hermes Agent](https://hermes-agent.nousresearch.com) 里的**进程内插件**——国内版（`workbuddy`）与国际版（`workbuddy-ai`）两个账户一起支持。
形态：**无服务、无端口、无开机自启**；凭据只读本机 WorkBuddy 桌面客户端的登录态。

## 一个统一包

按 Hermes 的 **unified package** 约定，整个功能就是**一个可安装文件夹**：

```
plugins/hermes-workbuddy/
├── plugin.yaml            # kind: model-provider
├── __init__.py            # 提供方（两个变体）+ register(ctx)：/workbuddy 命令
├── dashboard/             # 面板后端 → /api/plugins/hermes-workbuddy/
│   ├── manifest.json
│   └── plugin_api.py      # GET /snapshot：账号 / 模型倍率 / 额度
└── desktop/plugin.js      # 桌面半边（Electron 自动复制到 desktop-plugins/）
```

## 安装

```bash
hermes plugins install January10/hermes-workbuddy
```

装完后各部件在哪生效：

| 部件 | 在哪 | 怎么激活 |
| --- | --- | --- |
| 提供方（`workbuddy` / `workbuddy-ai`） | 模型选择器 / `/model` | 装完即被识别（`kind: model-provider`） |
| `/workbuddy` 命令 + `hermes workbuddy` CLI | 会话输入框 / 终端 | 同上；未生效则在 Capabilities → Plugins 启用一次 |
| 桌面页（模型表 + 额度） | 桌面 App 的 WorkBuddy 页面 | Capabilities → Plugins → **Rescan**（桌面半边是副本） |
| 面板后端 `/api/plugins/hermes-workbuddy/snapshot` | 桌面页数据源 | 随网关重启 |

改完代码生效：**⌘K → 重启网关**（提供方插件的 Python 不被热加载）。

## 额度显示口径

- **总分** = 所有积分包剩余之和（等于上游 `TotalDosage`）
- **N 天内到期** = 该窗口内到期且**仍可用**的分数之和
- 不展示包名 / 包明细 / 包数量

## 凭据

只读本机桌面客户端的登录态，**从不写入桌面状态**；仓库内**不含任何密钥或账号信息**，全部运行时读取。
两个变体分别对应国内版与国际版客户端：哪个没登录，就只少那一个账户的数据。

## 已知边界

- `kind: model-provider` 的包会被 `hermes plugins list` 当普通插件列出，但 **`plugins.enable/disable` 对它不起作用**——提供方发现路径不读 `plugins.enabled`（Hermes 自身的缝）。
- 桌面半边运行的是 `desktop-plugins/<id>/` 里的**副本**，不是本目录：改了 `desktop/plugin.js` 需要 Rescan 或重启才会同步。
