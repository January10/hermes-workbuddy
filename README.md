# hermes-workbuddy

WorkBuddy 模型在 [Hermes Agent](https://hermes-agent.nousresearch.com) 里的**进程内插件**实现（国内版 + 国际版两个账号）。
形态：**无服务、无端口、无开机自启** —— 插件跑在 Hermes 进程里，凭据只读本机桌面客户端登录态。

## 目录

| 路径 | 作用 |
| --- | --- |
| `plugins/model-providers/workbuddy/` | 提供方主体：模型目录（`data.models[]`）、倍率/状态、账号额度、用量钩子 |
| `plugins/workbuddy-models/` | 模型清单辅助插件 |
| `plugins/workbuddy-panel/dashboard/` | 面板后端 `GET /snapshot`（账号 / 模型 / 额度，30 秒缓存） |
| `desktop-plugins/workbuddy-panel/` | 桌面页：模型表（倍率、上下文、能力、推理档位）+ 额度行 |

## 额度显示口径

- **总分** = 所有积分包剩余之和（等于上游 `TotalDosage`）
- **N 天内到期** = 该窗口内到期且**仍可用**的分数之和
- 不展示包名 / 包明细 / 包数量

## 安装

把这些目录放到 Hermes 数据目录下同名位置（`%LOCALAPPDATA%\hermes\...`），重启网关后生效：

```
plugins/model-providers/workbuddy/   -> %LOCALAPPDATA%\hermes\plugins\model-providers\workbuddy\
plugins/workbuddy-models/           -> %LOCALAPPDATA%\hermes\plugins\workbuddy-models\
plugins/workbuddy-panel/            -> %LOCALAPPDATA%\hermes\plugins\workbuddy-panel\
desktop-plugins/workbuddy-panel/    -> %LOCALAPPDATA%\hermes\desktop-plugins\workbuddy-panel\
```

## 说明

- 凭据来源：本机 WorkBuddy 桌面客户端的登录态（只读，从不写入桌面状态）。
- 仓库内**不含任何密钥或账号信息**，全部运行时读取。
