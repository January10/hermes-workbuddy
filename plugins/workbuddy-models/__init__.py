"""WorkBuddy 模型面板（Hermes 通用插件）。

会话内 `/workbuddy`、CLI `hermes workbuddy-models`：一次列出**两类账户**各自的模型、
**积分倍率**、上下文、能力与额度 —— 也就是 DSH 那张卡片上的信息，搬进 Hermes 自己的界面。

* `workbuddy`    = 国内版客户端（企业额度）
* `workbuddy-ai` = 国际版客户端（积分资源包）

数据全部来自 model-providers/workbuddy 那个提供方插件（同一进程，无额外进程/端口）：
``fetch_models()`` 会顺带把倍率表、能力表与推理档位存回 profile，``fetch_account_usage()`` 取额度。

参数：`/workbuddy`（两个变体全看）· `cn` / `ai`（只看一个）· `models`（只看模型）· `usage`（只看额度）
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

VARIANT_ALIASES = {
    "cn": "workbuddy",
    "workbuddy": "workbuddy",
    "国内版": "workbuddy",
    "ai": "workbuddy-ai",
    "global": "workbuddy-ai",
    "workbuddy-ai": "workbuddy-ai",
    "国际版": "workbuddy-ai",
}
ALL_PROVIDERS = ("workbuddy", "workbuddy-ai")


# ── 数据 ─────────────────────────────────────────────────────────────────────

def _profile(provider: str):
    """活着的提供方 profile；没有该插件时返回 None。"""
    try:
        from providers import get_provider_profile

        return get_provider_profile(provider)
    except Exception:
        logger.debug("workbuddy-models: profile %s unavailable", provider, exc_info=True)
        return None


def _rate_number(text: Any) -> float | None:
    """``"x0.34 credits"`` → ``0.34``；没有数字则 None。"""
    digits = ""
    for char in str(text or ""):
        if char.isdigit() or char == ".":
            digits += char
        elif digits:
            break
    if not digits:
        return None
    try:
        return float(digits)
    except ValueError:
        return None


def _format_rate(text: Any) -> str:
    value = _rate_number(text)
    if value is None:
        return "—"
    return "免费" if value == 0 else f"×{value:g}"


def _format_context(tokens: Any) -> str:
    if not isinstance(tokens, int) or tokens <= 0:
        return "—"
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:g}M"
    if tokens >= 1000:
        return f"{tokens // 1000}k"
    return str(tokens)


def _snapshot(provider: str) -> dict[str, Any]:
    profile = _profile(provider)
    if profile is None:
        return {"ok": False, "provider": provider, "error": f"没找到提供方 {provider}（plugins/model-providers/workbuddy）"}
    labels = {"workbuddy": "国内版", "workbuddy-ai": "国际版"}
    models: list[str] = []
    live = True
    try:
        models = list(profile.fetch_models() or [])
    except Exception:
        logger.debug("workbuddy-models: catalog fetch failed for %s", provider, exc_info=True)
        live = False
    if not models:
        models = [str(model) for model in (profile.fallback_models or ())]
        live = False
    usage = None
    try:
        usage = profile.fetch_account_usage()
    except Exception:
        logger.debug("workbuddy-models: usage fetch failed for %s", provider, exc_info=True)
    account: dict[str, Any] = {}
    try:
        account = dict(profile.describe_account() or {})
    except Exception:
        logger.debug("workbuddy-models: account snapshot failed for %s", provider, exc_info=True)
    return {
        "ok": True,
        "provider": provider,
        "label": f"{profile.display_name}（{labels.get(provider, provider)}）",
        "models": models,
        "rates": dict(getattr(profile, "model_credits", {}) or {}),
        "capabilities": dict(getattr(profile, "model_capabilities", {}) or {}),
        "efforts": dict(getattr(profile, "model_efforts", {}) or {}),
        "usage": usage,
        "account": account,
        "live": live,
    }


# ── 渲染 ─────────────────────────────────────────────────────────────────────

def _models_block(snapshot: dict[str, Any]) -> str:
    models, rates = snapshot["models"], snapshot["rates"]
    capabilities = snapshot.get("capabilities") or {}
    efforts = snapshot.get("efforts") or {}
    ordered = sorted(models, key=lambda mid: (_rate_number(rates.get(mid)) is None, _rate_number(rates.get(mid)) or 0.0, mid))
    width = max((len(m) for m in ordered), default=8)
    lines = [f"{snapshot['label']} · 共 {len(ordered)} 个模型（倍率 = 消费积分的倍数，×0 = 免费）"]
    for mid in ordered:
        caps = capabilities.get(mid) or {}
        flags = []
        if caps.get("supports_tools"):
            flags.append("工具")
        if caps.get("supports_vision"):
            flags.append("图像")
        if caps.get("supports_reasoning"):
            levels = efforts.get(mid)
            flags.append("推理" + (f"[{'/'.join(levels)}]" if levels else ""))
        flag_text = "+".join(flags) if flags else "—"
        lines.append(
            f"  {mid.ljust(width)}  {_format_rate(rates.get(mid)).ljust(6)}"
            f"  上下文 {_format_context(caps.get('context_window')).ljust(5)} {flag_text}"
        )
    if not snapshot.get("live"):
        lines.append("  （上游目录拿不到，这是内置的回退清单）")
    return "\n".join(lines)


def _usage_block(snapshot: dict[str, Any]) -> str:
    usage = snapshot.get("usage")
    if usage is None:
        return f"{snapshot['label']} 额度：取不到（桌面端没登录或上游没回应）。"
    lines = [f"{snapshot['label']} 额度"]
    for window in usage.windows:
        remaining = "—" if window.used_percent is None else f"{round(100 - window.used_percent, 1)}%"
        lines.append(f"  {window.label}：{window.detail}（剩余 {remaining}）")
    for detail in usage.details:
        lines.append(f"  {detail}")
    return "\n".join(lines)


def _account_block(snapshot: dict[str, Any]) -> str:
    account = snapshot.get("account") or {}
    if not account:
        return f"{snapshot['label']} 账号：读不到（提供方插件里没有 describe_account）。"
    lines = [f"{snapshot['label']} 账号"]
    state = "已登录" if account.get("signed_in") else "未登录"
    who = account.get("nickname") or account.get("display_name") or "—"
    suffix = f"（{account['enterprise_name']}）" if account.get("enterprise_name") else ""
    lines.append(f"  {state}：{who}{suffix}")
    if account.get("uid"):
        uid_line = f"  uid：{account['uid']}"
        if account.get("account_type"):
            uid_line += f" · 类型 {account['account_type']}"
        lines.append(uid_line)
    if account.get("domain"):
        lines.append(f"  domain：{account['domain']}")
    if account.get("enterprise_id"):
        lines.append(f"  企业：{account.get('enterprise_name') or '—'}（id {account['enterprise_id']}）")
    if account.get("expires_at"):
        lines.append(f"  令牌到期：{account['expires_at']}（桌面端自动续期；401 时 `hermes auth refresh {snapshot['provider']}`）")
    if account.get("source"):
        lines.append(f"  凭据来源：{account['source']}")
    if account.get("auth_file"):
        lines.append(f"  登录文件：{account['auth_file']}")
    return "\n".join(lines)


def _resolve_providers(raw: str) -> tuple[list[str], str]:
    """把参数解析成 (providers, mode)；解析失败时 providers 为空、mode 是提示文本。"""
    args = [part for part in (raw or "").lower().replace("，", " ").split() if part]
    mode = "all"
    providers: list[str] = []
    for arg in args:
        if arg in ("usage", "额度", "积分"):
            mode = "usage"
        elif arg in ("models", "模型"):
            mode = "models"
        elif arg in ("account", "账号", "账户"):
            mode = "account"
        elif arg in VARIANT_ALIASES:
            providers.append(VARIANT_ALIASES[arg])
        else:
            return [], f"不认识的参数：{arg}（可用：cn | ai | account | models | usage）"
    return (providers or list(ALL_PROVIDERS)), mode


def _render_variant(provider: str, mode: str) -> list[str]:
    snapshot = _snapshot(provider)
    if not snapshot.get("ok"):
        return [str(snapshot.get("error"))]
    if mode == "usage":
        return [_usage_block(snapshot)]
    if mode == "models":
        return [_models_block(snapshot)]
    if mode == "account":
        return [_account_block(snapshot)]
    return [_account_block(snapshot), _models_block(snapshot), _usage_block(snapshot)]


def _render(raw: str = "") -> str:
    providers, mode = _resolve_providers(raw)
    if not providers:
        return mode
    blocks: list[str] = []
    for provider in providers:
        blocks.extend(_render_variant(provider, mode))
    return "\n\n".join(blocks)


# ── 注册 ─────────────────────────────────────────────────────────────────────

def _handle_command(raw_args: str) -> str:
    """``/workbuddy [cn|ai|models|usage]`` —— 输出回到会话里。"""
    try:
        return _render(raw_args or "")
    except Exception as exc:  # never take the session down over a readout
        logger.debug("workbuddy-models: render failed", exc_info=True)
        return f"WorkBuddy 面板出错：{type(exc).__name__}: {exc}"


def _setup_cli(parser) -> None:
    parser.add_argument("mode", nargs="?", default="", help="all | cn | ai | models | usage")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")


def _json_payload(provider: str) -> dict[str, Any]:
    snapshot = _snapshot(provider)
    if not snapshot.get("ok"):
        return {"provider": provider, "ok": False, "error": snapshot.get("error")}
    usage = snapshot.get("usage")
    rates = snapshot.get("rates") or {}
    capabilities = snapshot.get("capabilities") or {}
    efforts = snapshot.get("efforts") or {}
    return {
        "provider": provider,
        "label": snapshot.get("label"),
        "ok": True,
        "live": snapshot.get("live"),
        "account": snapshot.get("account") or {},
        "models": [
            {
                "id": mid,
                "credits": rates.get(mid),
                "multiplier": _rate_number(rates.get(mid)),
                "context_window": (capabilities.get(mid) or {}).get("context_window"),
                "supports_tools": (capabilities.get(mid) or {}).get("supports_tools"),
                "supports_vision": (capabilities.get(mid) or {}).get("supports_vision"),
                "reasoning_efforts": list(efforts.get(mid) or ()),
            }
            for mid in snapshot.get("models", [])
        ],
        "credits": None
        if usage is None
        else {
            "title": usage.title,
            "windows": [
                {"label": w.label, "used_percent": w.used_percent, "detail": w.detail} for w in usage.windows
            ],
            "details": list(usage.details),
        },
    }


def _handle_cli(args) -> int:
    if getattr(args, "json", False):
        import json

        providers, mode = _resolve_providers(getattr(args, "mode", "") or "")
        if not providers:
            print(mode)
            return 2
        print(json.dumps([_json_payload(provider) for provider in providers], ensure_ascii=False, indent=1))
        return 0
    print(_render(getattr(args, "mode", "") or ""))
    return 0


def register(ctx) -> None:
    """注册会话内斜杠命令与同名 CLI 子命令。"""
    ctx.register_command(
        "workbuddy",
        _handle_command,
        description="WorkBuddy 两类账户的账号、模型、积分倍率与额度",
        args_hint="[cn|ai|account|models|usage]",
    )
    try:
        ctx.register_cli_command(
            "workbuddy-models",
            "列出 WorkBuddy（国内版/国际版）的模型、倍率与额度",
            _setup_cli,
            _handle_cli,
        )
    except Exception:
        logger.debug("workbuddy-models: CLI command registration skipped", exc_info=True)
