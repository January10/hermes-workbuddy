# hermes-workbuddy

WorkBuddy 模型在 [Hermes Agent](https://hermes-agent.nousresearch.com) 里的**进程内插件**——国内版（`workbuddy`）与国际版（`workbuddy-ai`）两个账户一起支持。
形态：**无服务、无端口、无开机自启**；凭据只读本机 WorkBuddy 桌面客户端的登录态。

## 两个包（一个仓库）

Hermes 规定 `kind: model-provider` 的插件**只走 providers/ 发现、不被插件管理器加载**（避免双重导入破坏提供方覆盖关系），
所以"提供方"和"命令"两半必须分属两个包——一个仓库，两条安装命令：

```
hermes-workbuddy/            # 仓库根 = 提供方包
├── plugin.yaml              # kind: model-provider
├── __init__.py              # 两个提供方变体（workbuddy / workbuddy-ai）
├── dashboard/               # 面板后端 → /api/plugins/hermes-workbuddy/
│   ├── manifest.json
│   └── plugin_api.py        # GET /snapshot：账号 / 模型倍率 / 额度
├── desktop/plugin.js        # 桌面半边（Electron 自动复制到 desktop-plugins/）
└── cmd/                     # 命令子包（第二条安装命令）
    ├── plugin.yaml          # hermes-workbuddy-cmd
    └── __init__.py          # /workbuddy 会话命令 + hermes workbuddy CLI
```

## 安装

```bash
hermes plugins install January10/hermes-workbuddy        # 提供方 + 面板后端 + 桌面页
hermes plugins install January10/hermes-workbuddy#cmd    # /workbuddy 命令 + CLI（可选）
```

装完后各部件在哪生效：

| 部件 | 在哪 | 怎么激活 |
| --- | --- | --- |
| 提供方（`workbuddy` / `workbuddy-ai`） | 模型选择器 / `/model` | 装完即被识别（`kind: model-provider`） |
| 面板后端 `/api/plugins/hermes-workbuddy/snapshot` | 桌面页数据源 | 随网关重启 |
| 桌面页（模型表 + 额度） | 桌面 App 的 WorkBuddy 页面 | Capabilities → Plugins → **Rescan** + 打开开关（桌面半边是副本，默认关） |
| `/workbuddy` 命令 + `hermes workbuddy` CLI | 会话输入框 / 终端 | 第二条命令装完后即用 |

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
