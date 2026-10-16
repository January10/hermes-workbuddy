"""WorkBuddy 模型提供方 —— 同时接国内版与国际版两个本地登录（进程内，无服务）。

Hermes 里对应 DSH 的 ``dsh-workbuddy-connect``：它检测本机的**两类账户**并注册两个提供方，
这里一样 ——

* ``workbuddy``    → 国内版客户端 ``workbuddy-desktop.info``，上游 ``copilot.tencent.com``
* ``workbuddy-ai`` → 国际版客户端 ``workbuddy-desktop-ai.info``，上游 ``www.workbuddy.ai``

区别只在变体：登录文件名、上游主机（含 Origin/Referer 与计费主机）、User-Agent 形状、
以及国际版多两条 body 规则（首条必须是 system、``reasoning_effort: off`` 要丢掉）。
其余（凭据只读、目录与倍率解析、凭据池轮换、流式要求）两个变体共用一套代码。

上游契约（实测自 DSH 插件与本机两个账号）：

* ``stream`` 必须为 true —— 缓冲请求会被网关以 ``400/11101`` 拒（Hermes 主路径本就流式，
  辅助调用需要在 config.yaml 里声明 ``auxiliary.stream_only_base_urls``）。
* ``role: "developer"`` 会被拒 —— 统一折回 ``system``。
* 国际版首条消息必须是 ``system``（否则 ``400/11128``）。
* ``max_tokens`` 低于 16 会被拒（``400/11133``）。
* 目录在 ``/v3/config``（不是 ``/models``）；"可对话"名单取自 ``data.agents[name=cli].models``，
  再用 ``maxInputTokens``/``maxOutputTokens`` 都 > 0 的行过滤（图像/视频行天然被剔除）。
* 额度：国际版 ``/v2/billing/meter/get-user-resource``（资源包列表）；
  国内版 ``/v2/billing/meter/get-enterprise-user-usage``（企业周期额度，``limitNum: -1`` = 不限量）。
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlparse

from providers import register_provider
from providers.base import ProviderProfile

logger = logging.getLogger(__name__)

SIGN_IN_DIR = ("CodeBuddyExtension", "Data", "Public", "auth")
CLI_UA = "CLI/2.63.2 CodeBuddy/2.63.2"
FALLBACK_APP_VERSION = "5.5.2"
MAX_TOKENS_FLOOR = 16
HTTP_TIMEOUT = 8.0
BILLING_PRODUCT_CODE = "p_tcaca"
RESOURCE_PRODUCT_CODE = BILLING_PRODUCT_CODE   # get-user-resource 的 ProductCode
# 包到期窗口：DSH 用 3185136000 "毫秒"（实际按秒算 ≈ 36.9 天），会把到期更远的包漏掉
# （实测 37 天窗口漏了 10-31 到期的 500 分体验包 → 2884；窗口放到 3000 天 → 70 个包 = 3384，
# 与桌面端卡片的数字一致）。这里直接取一个"全都算进来"的窗口。
RESOURCE_WINDOW_MS = 3000 * 24 * 3600 * 1000          # 3000 天（实际上不设限）
RESOURCE_PAGE_SIZE = 500
BILLING_USER_AGENT = CLI_UA  # 计费端点的 UA 就是 CLI 那个（DSH 用的是同一个常量）
SIGN_IN_HELP = "本机桌面版还没登录：打开对应的 WorkBuddy 桌面客户端登录一次。"


# ── 两个变体 ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Variant:
    """一台机器上的两类 WorkBuddy 账户：国内版（CN）与国际版（AI）。"""

    id: str                    # provider id，与 DSH 插件一致
    label: str                 # 展示名
    region: str                # "cn" | "global"
    auth_filename: str         # 桌面登录文件名
    auth_env: str              # 覆盖登录文件路径的环境变量
    chat_base: str             # 对话上游（含 /v2/chat/completions）
    web_base: str              # Origin/Referer 主机 + 计费主机
    app_name: str              # User-Agent 里的产品名
    aux_model: str             # 辅助任务用的便宜模型
    fallback_models: tuple     # 目录抓取失败时的回退名单（实测过的 cli 名单）


CN = Variant(
    id="workbuddy",
    label="WorkBuddy",
    region="cn",
    auth_filename="workbuddy-desktop.info",
    auth_env="WORKBUDDY_AUTH_FILE",
    chat_base="https://copilot.tencent.com",
    web_base="https://www.codebuddy.cn",
    app_name="WorkBuddy",
    aux_model="hy3",
    fallback_models=(
        "hy4-preview", "hy3", "hy3-x", "step-5-preview", "space-bunny",
        "deepseek-v4.1-flash", "deepseek-v4-pro", "glm-5.3", "glm-5.3-flash", "glm-5.2",
        "glm-5.1", "glm-5v-turbo", "minimax-m3", "minimax-m2.7",
        "kimi-k3-1", "kimi-k2.8-preview", "kimi-k2.7", "kimi-k2.6", "mimo-v2.6-pro",
    ),
)

GLOBAL = Variant(
    id="workbuddy-ai",
    label="WorkBuddy AI",
    region="global",
    auth_filename="workbuddy-desktop-ai.info",
    auth_env="WORKBUDDY_AI_AUTH_FILE",
    chat_base="https://www.workbuddy.ai",
    web_base="https://www.workbuddy.ai",
    app_name="WorkBuddy AI",
    aux_model="deepseek-v4.1-flash",
    fallback_models=(
        "default-model", "fast-model", "balanced-model", "primary-model", "deep-model",
        "gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5", "gpt-5.4",
        "deepseek-v4.1-flash", "deepseek-v4.1-flash-sg", "glm-5.3", "glm-5.2",
        "kimi-k3", "kimi-k2.6", "kimi-k2.8-preview", "gemini-3.5-flash", "hy4-preview", "hy3",
    ),
)

VARIANTS = (CN, GLOBAL)
VARIANTS_BY_ID = {variant.id: variant for variant in VARIANTS}


# ── 桌面登录（只读） ──────────────────────────────────────────────────────────

def _auth_file(variant: Variant) -> Path:
    override = os.environ.get(variant.auth_env, "").strip()
    if override:
        return Path(override)
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local).joinpath(*SIGN_IN_DIR, variant.auth_filename)


def _as_ms(value: Any) -> int | None:
    """``expiresAt`` 可能是秒或毫秒，统一成毫秒。"""
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        return None
    number = float(value)
    return int(number * 1000) if number < 1e11 else int(number)


def _desktop_record(variant: Variant) -> dict[str, Any]:
    """该变体当前的桌面登录；缺失/登出/读不动时返回 ``{}``（从不写入）。"""
    path = _auth_file(variant)
    try:
        if Path(f"{path}.logged-out").exists():
            return {}
        record = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    auth = record.get("auth") if isinstance(record, dict) else None
    if not isinstance(auth, dict):
        return {}
    token = auth.get("accessToken")
    if not isinstance(token, str) or not token.strip():
        return {}
    account = record.get("account") if isinstance(record.get("account"), dict) else {}
    domain = auth.get("domain")
    refresh_token = auth.get("refreshToken")
    enterprise = account.get("enterpriseId")
    return {
        "access_token": token.strip(),
        "refresh_token": refresh_token if isinstance(refresh_token, str) else "",
        "uid": account.get("uid") if isinstance(account.get("uid"), str) else "",
        "nickname": account.get("nickname") if isinstance(account.get("nickname"), str) else "",
        "display_name": account.get("displayName") if isinstance(account.get("displayName"), str) else "",
        "domain": domain if isinstance(domain, str) and domain else "",
        "enterprise_id": enterprise if isinstance(enterprise, str) else "",
        "enterprise_name": account.get("enterpriseName") if isinstance(account.get("enterpriseName"), str) else "",
        "enterprise_user": account.get("enterpriseUserName") if isinstance(account.get("enterpriseUserName"), str) else "",
        "account_type": account.get("type") if isinstance(account.get("type"), str) else "",
        "expires_at_ms": _as_ms(auth.get("expiresAt")),
    }


def _token_env_name(variant: Variant) -> str:
    return f"{variant.id.upper().replace('-', '_')}_ACCESS_TOKEN"


def _credential(variant: Variant, api_key: str | None = None) -> dict[str, Any]:
    """一次调用用的凭据：显式/环境令牌优先，否则现读桌面登录。"""
    record = _desktop_record(variant)
    token = (api_key or os.environ.get(_token_env_name(variant), "") or "").strip()
    if token:
        source = "environment"
    else:
        token = record.get("access_token", "")
        source = "desktop-login"
    if not token:
        return {}
    merged = dict(record)
    merged["access_token"] = token
    merged.setdefault("domain", "workbuddy.ai" if variant.region == "global" else "")
    merged.setdefault("uid", "")
    merged.setdefault("enterprise_id", "")
    merged["source"] = source
    return merged


def _app_version() -> str:
    """UA 里的客户端版本：DSH 目录缓存里的，否则用已知可用的常量。"""
    path = Path(os.environ.get("DSH_HOME") or Path.home() / ".dsh") / ".workbuddy-ai-catalog.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return FALLBACK_APP_VERSION
    entries = payload.get("entries") if isinstance(payload, dict) else None
    for entry in (entries or {}).values() if isinstance(entries, dict) else ():
        version = entry.get("appVersion") if isinstance(entry, dict) else None
        if isinstance(version, str) and version.strip():
            return version.strip()
    return FALLBACK_APP_VERSION


def _chat_user_agent(variant: Variant) -> str:
    """国内版用 CLI 身份；国际版用桌面 App 身份。"""
    if variant.region == "cn":
        return CLI_UA
    version = _app_version()
    return f"WorkBuddy/{version} {variant.app_name}/{version}"


def _catalog_user_agents(variant: Variant) -> tuple[str, ...]:
    if variant.region == "cn":
        return (CLI_UA,)
    version = _app_version()
    return (f"WorkBuddyAI/{version}", f"WorkBuddy/{version} {variant.app_name}/{version}")


def _headers(variant: Variant, cred: dict[str, Any], *, user_agent: str | None = None) -> dict[str, str]:
    """两个变体共用的请求头形状（差异只在 UA、Origin/Referer 与企业头）。"""
    headers = {
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": variant.web_base,
        "Referer": f"{variant.web_base}/",
        "User-Agent": user_agent or _chat_user_agent(variant),
        "X-Product": "SaaS",
        "X-IDE-Type": "WorkBuddy",
        "X-IDE-Name": "WorkBuddy",
        "X-IDE-Version": _app_version(),
    }
    uid = str(cred.get("uid") or "")
    headers["X-User-Id" if uid else "X-No-User-Id"] = uid or "1"
    domain = str(cred.get("domain") or "")
    headers["X-Domain" if domain else "X-No-Department-Info"] = domain or "1"
    enterprise = str(cred.get("enterprise_id") or "")
    headers["X-Enterprise-Id" if enterprise else "X-No-Enterprise-Id"] = enterprise or "1"
    return headers


def _billing_headers(cred: dict[str, Any], *, origin: str = "") -> dict[str, str]:
    """计费端点的请求头 —— **这几个头是一组**，缺一个就 ``403 请求不合法``。

    实测（个人号，同一凭据）：只带 ``Authorization`` → 403；``Authorization`` + ``X-User-Id``
    → 403；``Authorization`` + ``X-Requested-With`` → 403；把它们与 ``Origin``/``Referer``/
    浏览器式 ``Accept``/``User-Agent`` 一起发 → 200。DSH 的 ``dsh-workbuddy-connect`` 也是这套。
    """
    headers = {
        "Authorization": f"Bearer {cred['access_token']}",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
    }
    if origin:
        headers["Origin"] = origin
        headers["Referer"] = origin + "/"
    headers["User-Agent"] = BILLING_USER_AGENT
    if cred.get("uid"):
        headers["X-User-Id"] = str(cred["uid"])
    if cred.get("enterprise_id"):
        headers["X-Enterprise-Id"] = str(cred["enterprise_id"])
        headers["X-Tenant-Id"] = str(cred["enterprise_id"])
    if cred.get("domain"):
        headers["X-Domain"] = str(cred["domain"])
    return headers


def _resource_body() -> dict[str, Any]:
    """``get-user-resource`` 的载荷：未来 37 天的在用/待用资源包（窗口按 DSH 实测值）。"""
    from datetime import datetime, timedelta

    now = datetime.now()
    end = now + timedelta(milliseconds=RESOURCE_WINDOW_MS)
    fmt = "%Y-%m-%d %H:%M:%S"
    return {
        "PageNumber": 1,
        "PageSize": RESOURCE_PAGE_SIZE,
        "ProductCode": RESOURCE_PRODUCT_CODE,
        "Status": [0, 3],
        "PackageEndTimeRangeBegin": now.strftime(fmt),
        "PackageEndTimeRangeEnd": end.strftime(fmt),
    }


def _number_from(source: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = source.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _packages_from_resource(body: Any) -> list[dict[str, Any]]:
    """``get-user-resource`` 响应 → 资源包列表（两个变体同一形状）。

    形状：``data.Response.Data.Accounts[]``，每行自带 ``CycleCapacitySize/Remain/Used``
    （本周期）、``CapacitySize/Remain/Used``（总量）与 ``CycleEndTime``。
    剩余优先取周期的，没有周期字段再退回总量。
    """
    data = body.get("data") if isinstance(body, dict) else None
    response = data.get("Response") if isinstance(data, dict) else None
    inner = response.get("Data") if isinstance(response, dict) else None
    rows = inner.get("Accounts") if isinstance(inner, dict) else None
    if not isinstance(rows, list):
        return []
    packages: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        size = _number_from(row, "CycleCapacitySize", "CapacitySize")
        used = _number_from(row, "CycleCapacityUsed", "CapacityUsed")
        remain = _number_from(row, "CycleCapacityRemain", "CapacityRemain")
        if size is None and remain is None:
            continue
        size = int(size or 0)
        if remain is None:
            remain = max(0, size - int(used or 0))
        remain = max(0, int(remain))
        expires = str(row.get("CycleEndTime") or row.get("ExpiredTime") or "").strip()
        packages.append({
            "name": str(row.get("PackageName") or "资源包").strip() or "资源包",
            "size": size,
            "used": int(used or max(0, size - remain)),
            "remain": remain,
            "expires_at": expires,
        })
    return packages


# ── 目录解析 ─────────────────────────────────────────────────────────────────

def _models_from_config(
    payload: Any, variant: Variant
) -> tuple[list[str], dict[str, dict[str, Any]], dict[str, str], dict[str, tuple[str, ...]]]:
    """读 ``/v3/config``：cli 名单 ∩ 可用行；顺带把倍率、能力、推理档位学回来。

    ``data.modelPromotions[]`` 是活动标记（其 ``id`` 是活动键不是模型），所以只走
    ``data.models`` + ``data.agents``；拿不到 ``agents`` 时才退化成"in/out 都大于 0 的行"。
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    rows = data.get("models") if isinstance(data, dict) else None
    agents = data.get("agents") if isinstance(data, dict) else None
    membership: set[str] = set()
    if isinstance(agents, list):
        for agent in agents:
            if isinstance(agent, dict) and agent.get("name") == "cli" and isinstance(agent.get("models"), list):
                membership = {str(item) for item in agent["models"] if isinstance(item, str)}
                break

    ids: list[str] = []
    capabilities: dict[str, dict[str, Any]] = {}
    credits: dict[str, str] = {}
    efforts: dict[str, tuple[str, ...]] = {}
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            model_id = row.get("id")
            if not isinstance(model_id, str) or not model_id.strip():
                continue
            model_id = model_id.strip()
            if row.get("disabled") is True:
                continue
            if membership and model_id not in membership:
                continue
            max_in = row.get("maxInputTokens")
            max_out = row.get("maxOutputTokens")
            if not (isinstance(max_in, int) and max_in > 0 and isinstance(max_out, int) and max_out > 0):
                continue
            ids.append(model_id)
            entry: dict[str, Any] = {"supports_tools": True}
            if row.get("supportsImages") is True and row.get("disabledMultimodal") is not True:
                entry["supports_vision"] = True
            context = max_in
            if variant.region == "global":
                window = row.get("contextWindow")
                if isinstance(window, dict) and isinstance(window.get("defaultLength"), int) and window["defaultLength"] > 0:
                    context = window["defaultLength"]
            entry["context_window"] = context
            # 目录声明的输出上限（"强度拉满"要用的就是它：Hermes 默认不会替我们猜）。
            if isinstance(max_out, int) and max_out > 0:
                entry["max_output_tokens"] = max_out
            if isinstance(row.get("maxAllowedSize"), int) and row["maxAllowedSize"] > 0:
                entry["max_allowed_size"] = row["maxAllowedSize"]
            reasoning = row.get("reasoning") if isinstance(row.get("reasoning"), dict) else None
            if row.get("supportsReasoning") is True or (reasoning and reasoning.get("supportedEfforts")):
                entry["supports_reasoning"] = True
            capabilities[model_id] = entry
            rate = row.get("credits")
            if isinstance(rate, str) and rate.strip():
                credits[model_id] = rate.strip()
            levels = reasoning.get("supportedEfforts") if reasoning else None
            if isinstance(levels, list):
                supported = tuple(str(item) for item in levels if isinstance(item, str) and item.strip())
                if supported:
                    efforts[model_id] = supported
    if ids:
        return ids, capabilities, credits, efforts

    found: dict[str, None] = {}

    def visit(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, dict):
            return
        candidate = node.get("id") or node.get("model") or node.get("modelId")
        if isinstance(candidate, str) and candidate.strip() and candidate not in found:
            found[candidate.strip()] = None
        for value in node.values():
            visit(value)

    visit(payload)
    return list(found), capabilities, credits, efforts


def _cached_models(variant: Variant) -> list[str]:
    """DSH 插件的目录缓存（只在实时抓取失败时用）。"""
    name = ".workbuddy-ai-catalog.json" if variant.region == "global" else ".workbuddy-catalog.json"
    path = Path(os.environ.get("DSH_HOME") or Path.home() / ".dsh") / name
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    entries = payload.get("entries") if isinstance(payload, dict) else None
    for entry in (entries or {}).values() if isinstance(entries, dict) else ():
        models = entry.get("models") if isinstance(entry, dict) else None
        if isinstance(models, list) and models:
            return [str(model) for model in models if isinstance(model, str)]
    return []


def _rate_number(text: Any) -> float | None:
    """``"x0.34 credits"`` / ``"x0.00"`` → ``0.34`` / ``0.0``；没有数字则 ``None``。"""
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


def _format_rates(credits: dict[str, str]) -> str:
    """一行倍率表，便宜的在前：``模型倍率：a ×0.00 · b ×0.79``。"""
    ordered = sorted(
        ((mid, _rate_number(text)) for mid, text in credits.items() if _rate_number(text) is not None),
        key=lambda item: item[1] or 0.0,
    )
    if not ordered:
        return ""
    head = " · ".join(f"{mid} ×{value:g}" for mid, value in ordered[:8])
    tail = f" 等 {len(ordered)} 个模型" if len(ordered) > 8 else ""
    return f"模型倍率：{head}{tail}"


# ── 上游状态：限时促销、免费试用、免费路由 ──────────────────────────────────


def _iso_dt(text: Any):
    from datetime import datetime, timezone

    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _promo_active(promo: dict[str, Any]) -> bool:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    start = _iso_dt(promo.get("valid_from"))
    end = _iso_dt(promo.get("valid_until"))
    if start is not None and now < start:
        return False
    if end is not None and now > end:
        return False
    return True


def _statuses_from_config(payload: Any) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[str]]]:
    """把 ``/v3/config`` 里的"状态"学回来 → ``({model_id: [促销…]}, {model_id: [备注…]})``。

    ``data.modelPromotions[]`` 是官方的限时活动（国际版当前 3 条：``hy3``、
    ``deepseek-v4.1-flash``、``step-5-preview`` 都在限免期内）；国内版目前没有活动块，
    但会给"14 天免费试用"横幅与"免费路由/付费路由"这种备注。
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return {}, {}

    promotions: dict[str, list[dict[str, Any]]] = {}
    raw = data.get("modelPromotions")
    if isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, dict) or entry.get("enabled") is False:
                continue
            models = [
                str(item).strip() for item in (entry.get("modelIds") or []) if isinstance(item, str) and str(item).strip()
            ]
            if not models:
                continue
            badge = entry.get("badge") if isinstance(entry.get("badge"), dict) else {}
            discount = entry.get("discount") if isinstance(entry.get("discount"), dict) else {}
            hover = entry.get("hover") if isinstance(entry.get("hover"), dict) else {}
            schedule = entry.get("schedule") if isinstance(entry.get("schedule"), dict) else {}
            factor = discount.get("factor")
            promo: dict[str, Any] = {
                "id": str(entry.get("id") or "").strip(),
                "kind": str(entry.get("kind") or "").strip(),
                "label": str(badge.get("label") or "").strip(),
                "label_zh": str(badge.get("labelZh") or entry.get("labelZh") or "").strip(),
                "color": str(badge.get("color") or "").strip(),
                "factor": float(factor) if isinstance(factor, (int, float)) and not isinstance(factor, bool) else None,
                "credits_text": str(discount.get("discountedCredits") or "").strip(),
                "display_mode": str(discount.get("displayMode") or "").strip(),
                "text": str(hover.get("textZh") or hover.get("text") or hover.get("textEn") or "").strip(),
                "valid_from": str(schedule.get("validFrom") or "").strip(),
                "valid_until": str(schedule.get("validUntil") or "").strip(),
                "timezone": str(schedule.get("timezone") or "").strip(),
                "priority": float(entry.get("priority")) if isinstance(entry.get("priority"), (int, float)) else 0.0,
            }
            promo["active"] = _promo_active(promo)
            for model_id in models:
                promotions.setdefault(model_id, []).append(promo)

    notes: dict[str, list[str]] = {}
    features = data.get("productFeaturesConfig") if isinstance(data.get("productFeaturesConfig"), dict) else {}
    trial = features.get("ModelTrialBanner") if isinstance(features.get("ModelTrialBanner"), dict) else {}
    for banner in trial.get("banners") or []:
        if not isinstance(banner, dict):
            continue
        target = banner.get("targetModelId") or banner.get("modelId")
        days = banner.get("trialDays")
        if isinstance(target, str) and target.strip() and isinstance(days, (int, float)) and days > 0 and not isinstance(days, bool):
            notes.setdefault(target.strip(), []).append(f"首次使用起 {int(days)} 天免费试用")
    cap = features.get("ModelRateLimitCap") if isinstance(features.get("ModelRateLimitCap"), dict) else {}
    for line in cap.get("lines") or []:
        if not isinstance(line, dict):
            continue
        free_id = line.get("freeId")
        paid_id = line.get("paidId")
        if isinstance(free_id, str) and free_id.strip():
            paid = f"（超限可切付费 {paid_id}）" if isinstance(paid_id, str) and paid_id.strip() else ""
            notes.setdefault(free_id.strip(), []).append(f"免费路由{paid}")
    return promotions, notes


def _httpx():
    import httpx  # lazy: the profile must import in interpreters without the app's HTTP stack

    return httpx


# ── 额度整理：总计 + 3 天内到期 ──────────────────────────────────────────────

CREDIT_SOON_DAYS = 3


def _parse_ts(text: Any):
    from datetime import datetime

    raw = str(text or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _credit_summary(
    items: list[dict[str, Any]],
    *,
    unlimited: bool,
    cycle_end: str = "",
    reset: str = "",
    limit: float | None = None,
    used: float | None = None,
) -> dict[str, Any]:
    """额度摘要：**总分** + ``CREDIT_SOON_DAYS`` 天内到期的**可用分数**。

    用户的显示口径（明确要求）：不要包名、不要包明细，只要两个数字 ——
    总分（所有包剩余之和，等于上游 ``TotalDosage``）和 3 天内到期的可用分数之和。
    内部仍保留每个包的明细（``soon[]`` 带 name/size/expires_at），但界面不展示。
    """
    from datetime import datetime

    now = datetime.now()
    summary: dict[str, Any] = {
        "unlimited": bool(unlimited),
        "count": sum(1 for item in items if int(item.get("remain") or 0) > 0),
        "count_total": len(items),
        "total_remain": sum(int(item.get("remain") or 0) for item in items),
        "total_size": sum(int(item.get("remain") or 0) for item in items),
        "total_used": 0,
        "soon": [],
        "soon_remain": 0,
        "soon_days": CREDIT_SOON_DAYS,
        "cycle_end": cycle_end,
        "reset": reset,
    }
    if limit is not None and used is not None:
        summary["limit"] = limit
        summary["used"] = used
        summary["total_remain"] = max(0.0, float(limit) - float(used))
        summary["total_size"] = summary["total_remain"]
    soon_remain = 0
    for item in items:
        remain = int(item.get("remain") or 0)
        when = _parse_ts(item.get("expires_at"))
        if when is None:
            continue
        delta = (when - now).total_seconds()
        if not (0 <= delta <= CREDIT_SOON_DAYS * 86400):
            continue
        if remain <= 0:
            continue  # 已用完的包不算"可用分数"
        soon_remain += remain
        summary["soon"].append({
            "name": item["name"],
            "remain": remain,
            "size": int(item["size"]),
            "expires_at": item["expires_at"],
            "days_left": max(0, int(delta // 86400)),
        })
    summary["soon"].sort(key=lambda row: (row["expires_at"], -row["remain"]))
    summary["soon_remain"] = soon_remain
    return summary


def _credit_lines(summary: dict[str, Any]) -> list[str]:
    """人类可读版：**只给两个数字** —— 总分 + 3 天内到期的可用分数。

    不列包名/包明细（用户明确要求）。``soon`` 明细仍留在结构化数据里，界面想细看再用。
    """
    lines: list[str] = []
    if summary.get("unlimited"):
        lines.append("额度：不限量（企业版）" + (f" · 本周期至 {summary['cycle_end']}" if summary.get("cycle_end") else ""))
    else:
        lines.append(f"总分：{int(summary.get('total_remain') or 0)} 分")
        days = summary.get("soon_days", CREDIT_SOON_DAYS)
        lines.append(f"{days} 天内到期：{int(summary.get('soon_remain') or 0)} 分")
    if summary.get("reset"):
        lines.append(f"下次重置：{summary['reset']}")
    return lines


# ── 凭据池 ───────────────────────────────────────────────────────────────────

def _pool_entry(variant: Variant, cred: dict[str, Any]):
    from agent.credential_pool import AUTH_TYPE_API_KEY, PooledCredential

    return PooledCredential(
        provider=variant.id,
        id=uuid.uuid4().hex[:6],
        label=str(cred.get("nickname") or cred.get("uid") or "desktop-login"),
        auth_type=AUTH_TYPE_API_KEY,
        priority=0,
        # 必须以 manual: 开头 —— 否则 Hermes 会把 secret 判为"借用凭据"并在落盘时抹掉。
        source="manual:desktop-login",
        access_token=str(cred["access_token"]),
        refresh_token=cred.get("refresh_token") or None,
        expires_at_ms=cred.get("expires_at_ms"),
        extra={
            "uid": cred.get("uid", ""),
            "domain": cred.get("domain", ""),
            "enterprise_id": cred.get("enterprise_id", ""),
            "region": variant.region,
        },
    )


def _seed_pool(variant: Variant) -> bool:
    """凭据池没有该变体的行时，从桌面登录补一条（Hermes 才能轮换/复用）。"""
    cred = _credential(variant)
    if not cred.get("access_token"):
        return False
    try:
        from agent.credential_pool import load_pool

        pool = load_pool(variant.id)
        if pool.entries():
            return False
        pool.add_entry(_pool_entry(variant, cred))
        return True
    except Exception:
        logger.debug("workbuddy[%s]: pool seeding skipped", variant.id, exc_info=True)
        return False


def _post_refresh(variant: Variant, refresh_token: str) -> dict[str, Any]:
    """调上游轮换 grant（两个变体同一路径，主机不同）。"""
    httpx = _httpx()
    response = httpx.post(
        f"{variant.chat_base}/v2/plugin/auth/token/refresh",
        headers={
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": variant.web_base,
            "Referer": f"{variant.web_base}/",
            "User-Agent": _chat_user_agent(variant),
            "X-Refresh-Token": refresh_token,
            "X-Auth-Refresh-Source": "workbuddy",
        },
        timeout=HTTP_TIMEOUT,
    )
    envelope = response.json() if response.content else {}
    if response.status_code != 200 or not isinstance(envelope, dict) or envelope.get("code") not in (0, None):
        raise RuntimeError(f"{variant.id} token refresh failed: HTTP {response.status_code} {str(envelope)[:200]}")
    data = envelope.get("data") if isinstance(envelope.get("data"), dict) else {}
    access_token = data.get("accessToken")
    if not isinstance(access_token, str) or not access_token.strip():
        raise RuntimeError(f"{variant.id} token refresh returned no accessToken; sign in again in the desktop app")
    rotated: dict[str, Any] = {"access_token": access_token.strip()}
    if isinstance(data.get("refreshToken"), str) and data["refreshToken"].strip():
        rotated["refresh_token"] = data["refreshToken"].strip()
    expires_in = data.get("expiresIn")
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        rotated["expires_at_ms"] = int(time.time() * 1000) + int(expires_in * 1000)
    return rotated


def _make_refresh(variant: Variant):
    """凭据池的轮换钩子：桌面记录更新就用它，否则才打上游 refresh。"""

    def refresh(entry: Any):
        record = _desktop_record(variant)
        current = str(getattr(entry, "access_token", "") or "")
        desktop_token = record.get("access_token", "")
        if desktop_token and desktop_token != current:
            return {
                "access_token": desktop_token,
                "refresh_token": record.get("refresh_token") or getattr(entry, "refresh_token", None),
                "expires_at_ms": record.get("expires_at_ms"),
                "uid": record.get("uid", ""),
                "domain": record.get("domain", ""),
            }
        stored = record.get("refresh_token") or getattr(entry, "refresh_token", None) or ""
        if not stored:
            return None
        return _post_refresh(variant, str(stored))

    return refresh


def _describe(variant: Variant, record: dict[str, Any]) -> str:
    expires = record.get("expires_at_ms")
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(expires / 1000)) if expires else "未知"
    who = record.get("nickname") or record.get("uid") or "—"
    return f"{variant.label}（uid={who}, 到期 {when}, domain={record.get('domain') or '—'}）"


def _make_auth_handler(variant: Variant):
    """``hermes auth add|status|logout|refresh <variant.id>`` 的插件实现。"""

    def handler(action: str, args: Any) -> bool:
        from agent.credential_pool import load_pool

        if action == "add":
            record = _desktop_record(variant)
            if not record.get("access_token"):
                print(f"{variant.label}: {SIGN_IN_HELP}")
                return True
            try:
                load_pool(variant.id).add_entry(_pool_entry(variant, record))
            except Exception as exc:
                print(f"{variant.label}: 凭据写入失败：{type(exc).__name__}: {exc}")
                return True
            print(f"{variant.label}: 已从桌面登录写入凭据 {_describe(variant, record)}")
            return True

        if action == "status":
            record = _desktop_record(variant)
            rows = load_pool(variant.id).entries()
            if record.get("access_token"):
                print(f"{variant.label}: 桌面登录可用 {_describe(variant, record)}")
            else:
                print(f"{variant.label}: " + (f"凭据池里有 {len(rows)} 条记录" if rows else "未登录（见 README）"))
            if rows:
                live = [row for row in rows if str(getattr(row, "access_token", "")) == record.get("access_token", "")]
                print(f"  凭据池：{len(rows)} 条，其中 {len(live)} 条与本机登录一致")
            return True

        if action == "refresh":
            rows = load_pool(variant.id).entries()
            if not rows:
                print(f"{variant.label}: 凭据池是空的，先跑 `hermes auth add {variant.id}`。")
                return True
            try:
                rotated = _make_refresh(variant)(rows[0])
            except Exception as exc:
                print(f"{variant.label}: 刷新失败：{type(exc).__name__}: {exc}")
                return True
            if not rotated:
                print(f"{variant.label}: 本机登录记录没有变化，也没有可用的 refresh token。")
                return True
            load_pool(variant.id).reset_status(rows[0].id)
            print(f"{variant.label}: 已取到{'新的' if rotated.get('access_token') != rows[0].access_token else '同一'}令牌，下次请求会重新解析。")
            return True

        if action == "logout":
            pool = load_pool(variant.id)
            rows = pool.entries()
            for index in range(len(rows), 0, -1):
                pool.remove_index(index)
            print(f"{variant.label}: 已移除 {len(rows)} 条凭据（桌面端的登录不受影响）。")
            return True

        return False

    return handler


# ── 提供方 profile ───────────────────────────────────────────────────────────

class WorkBuddyProfile(ProviderProfile):
    """某个变体（国内版 / 国际版）的 profile；两个实例共用这一套钩子。"""

    variant: ClassVar[Variant] = CN
    # 实时学到的元数据：倍率与推理档位。桌面面板、/usage 与 README 都读这里。
    model_credits: dict[str, str] = {}
    model_efforts: dict[str, tuple[str, ...]] = {}
    # 上游"状态"：限时促销（限免/折扣）、试用横幅、免费路由备注。
    model_promotions: dict[str, list[dict[str, Any]]] = {}
    model_notes: dict[str, list[str]] = {}

    # ── 上下文 / 输出 / 推理强度：一律用目录声明的上限，不用 Hermes 的兜底默认 ──────
    # 说明（都对着 Hermes 的钩子实现看过）：
    # * ``get_model_context_length`` 在 ``agent/model_metadata.py`` 的解析链里排在缓存/探测之前，
    #   所以这里给的值就是压缩阈值用的窗口。
    # * ``get_max_tokens`` 由 ``agent/transports/chat_completions.py:_apply_max_tokens`` 采用，
    #   且仅在用户/任务没显式设 ``max_tokens`` 时生效。
    # * ``default_reasoning_config`` 由 ``agent/reasoning_params.py:unset_reasoning_default`` 采用，
    #   但**只在 ``agent.reasoning_effort`` 未设置时**走这条路；用户显式设过就让用户赢。

    def get_model_context_length(self, model: str) -> int | None:
        entry = self.model_capabilities.get(str(model or "")) or {}
        value = entry.get("context_window")
        return value if isinstance(value, int) and value > 0 else None

    def get_max_tokens(self, model: str | None) -> int | None:
        """目录里该模型的 ``maxOutputTokens``；拿不到才退回基类默认。"""
        entry = self.model_capabilities.get(str(model or "")) or {}
        value = entry.get("max_output_tokens")
        if isinstance(value, int) and value > 0:
            return value
        return super().get_max_tokens(model)

    def default_reasoning_config(self, model: str | None = None) -> dict | None:
        """未设置 effort 时，按该模型支持的档位取**最高档**（拿不到档位就不表态）。"""
        levels = self.model_efforts.get(str(model or ""))
        if not levels:
            return None
        return {"enabled": True, "effort": levels[-1]}

    def prepare_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """``developer`` → ``system``；国际版再保证首条是 system。"""
        if not isinstance(messages, list) or not messages:
            return messages
        prepared: list[dict[str, Any]] = []
        for message in messages:
            if isinstance(message, dict) and message.get("role") == "developer":
                prepared.append({**message, "role": "system"})
            else:
                prepared.append(message)
        if self.variant.region != "global":
            return prepared
        first = prepared[0] if prepared and isinstance(prepared[0], dict) else None
        if first is None or first.get("role") != "system":
            prepared.insert(0, {"role": "system", "content": "You are a helpful assistant."})
        return prepared

    def build_api_kwargs_extras(self, *, reasoning_config: dict | None = None, **context: Any):
        """wire 兜底：``max_tokens`` 下限（400/11133）；国际版的 effort 拼写规则。"""
        top_level: dict[str, Any] = {}
        max_tokens = context.get("max_tokens")
        if isinstance(max_tokens, int) and not isinstance(max_tokens, bool) and 0 < max_tokens < MAX_TOKENS_FLOOR:
            top_level["max_tokens"] = MAX_TOKENS_FLOOR
        effort = (reasoning_config or {}).get("effort") if isinstance(reasoning_config, dict) else None
        if self.variant.region == "global" and isinstance(effort, str) and effort.strip().lower() == "off":
            top_level["reasoning_effort"] = None
        return {}, top_level

    def supported_reasoning_efforts(self, model: str | None) -> tuple[str, ...] | None:
        """目录里声明的档位（热路径，只读缓存：``fetch_models`` 时学回来）。"""
        return self.model_efforts.get(str(model or ""))

    def model_status(self, model: str | None) -> str | None:
        """徽标用的短状态：``限免`` / ``免费`` / ``试用`` / ``限时 ×0.5`` / ``×0.79``。"""
        model_id = str(model or "")
        if not model_id:
            return None
        base = _rate_number(self.model_credits.get(model_id))
        active = [promo for promo in (self.model_promotions.get(model_id) or []) if promo.get("active")]
        if active:
            promo = max(active, key=lambda item: item.get("priority") or 0.0)
            factor = promo.get("factor")
            if factor == 0:
                return "限免"
            if factor is not None and (base is None or factor < base):
                return f"限时 ×{factor:g}"
        if base == 0:
            return "免费"
        if any("试用" in note for note in (self.model_notes.get(model_id) or [])):
            return "试用"
        if base is not None:
            return f"×{base:g}"
        return None

    def model_effective_multiplier(self, model: str | None) -> float | None:
        """实际扣分倍率：活动价优先，其次目录里的基础倍率。"""
        model_id = str(model or "")
        active = [promo for promo in (self.model_promotions.get(model_id) or []) if promo.get("active")]
        if active:
            promo = max(active, key=lambda item: item.get("priority") or 0.0)
            factor = promo.get("factor")
            if factor is not None:
                return float(factor)
        return _rate_number(self.model_credits.get(model_id))

    def model_status_detail(self, model: str | None) -> str:
        """页面/命令用的长文本：活动文案 + 有效期 + 备注。"""
        model_id = str(model or "")
        parts: list[str] = []
        for promo in self.model_promotions.get(model_id) or []:
            if not promo.get("active"):
                continue
            label = promo.get("label_zh") or promo.get("label") or "限时优惠"
            factor = promo.get("factor")
            rate = f" ×{factor:g}" if isinstance(factor, (int, float)) else ""
            window = f"（至 {promo['valid_until'][:10]}）" if promo.get("valid_until") else ""
            text = f"：{promo['text']}" if promo.get("text") else ""
            parts.append(f"{label}{rate}{window}{text}")
        parts.extend(self.model_notes.get(model_id) or [])
        return " · ".join(parts)

    def promotions_list(self) -> list[dict[str, Any]]:
        """活动清单（图例用）：把同一个活动挂着的模型并成一行，含未生效的。"""
        seen: dict[str, dict[str, Any]] = {}
        for model_id, promos in self.model_promotions.items():
            for promo in promos:
                key = str(promo.get("id") or "") or ",".join(sorted(promo.get("models") or [model_id]))
                row = seen.setdefault(key, {**promo, "models": []})
                if model_id not in row["models"]:
                    row["models"].append(model_id)
        return sorted(
            seen.values(),
            key=lambda item: (not (item.get("active") or False), -(item.get("priority") or 0.0), str(item.get("id"))),
        )

    def describe_account(self, *, api_key: str | None = None) -> dict[str, Any]:
        """账号快照（给 /workbuddy 面板用）：登录态、昵称、uid、企业、令牌到期、凭据来源。"""
        variant = self.variant
        record = _desktop_record(variant)
        cred = _credential(variant, api_key)
        expires = record.get("expires_at_ms")
        return {
            "provider": variant.id,
            "label": variant.label,
            "region": variant.region,
            "signed_in": bool(record.get("access_token")),
            "nickname": record.get("nickname", ""),
            "display_name": record.get("display_name", ""),
            "uid": record.get("uid", ""),
            "domain": record.get("domain", ""),
            "enterprise_name": record.get("enterprise_name", ""),
            "enterprise_id": record.get("enterprise_id", ""),
            "enterprise_user": record.get("enterprise_user", ""),
            "account_type": record.get("account_type", ""),
            "expires_at_ms": expires,
            "expires_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(expires / 1000)) if expires else "",
            "auth_file": str(_auth_file(variant)),
            "source": cred.get("source", ""),
        }

    def fetch_models(self, *, api_key: str | None = None, base_url: str | None = None, timeout: float = 8.0) -> list[str] | None:
        """该变体账号的目录：``GET {chat_base}/v3/config``（cli 名单 ∩ 可用行）。"""
        variant = self.variant
        _seed_pool(variant)
        cred = _credential(variant, api_key)
        if not cred.get("access_token"):
            logger.debug("workbuddy[%s]: no sign-in for the catalog probe", variant.id)
            return None
        base = (base_url or self.base_url or variant.chat_base).rstrip("/")
        origin = _origin(base) or variant.chat_base
        for user_agent in _catalog_user_agents(variant):
            try:
                httpx = _httpx()
                response = httpx.get(
                    f"{origin}/v3/config",
                    headers={**_headers(variant, cred, user_agent=user_agent),
                             "Authorization": f"Bearer {cred['access_token']}"},
                    timeout=min(timeout or HTTP_TIMEOUT, HTTP_TIMEOUT),
                )
                if response.status_code != 200:
                    continue
                payload = response.json()
                models, capabilities, credits, efforts = _models_from_config(payload, variant)
                if models:
                    promotions, notes = _statuses_from_config(payload)
                    try:
                        if capabilities:
                            self.model_capabilities.update(capabilities)
                        if credits:
                            self.model_credits = credits
                        if efforts:
                            self.model_efforts = efforts
                        if promotions:
                            self.model_promotions = promotions
                        if notes:
                            self.model_notes = notes
                    except Exception:
                        pass
                    return models
            except Exception:
                logger.debug("workbuddy[%s]: catalog probe failed", variant.id, exc_info=True)
        cached = _cached_models(variant)
        return cached or None

    def fetch_account_usage(self, *, api_key: str | None = None, base_url: str | None = None):
        """``/usage`` 的额度快照：国际版=资源包列表，国内版=周期额度。

        国内版接口要**显式带 uid**（企业号再带 enterpriseID）：只靠 ``X-User-Id`` 头，
        个人号会拿到 ``400 {"code":10001,"msg":"uid or enterpriseID is empty"}``
        （企业号看着能过，是因为桌面登录文件里带着 enterprise_id）。
        """
        variant = self.variant
        cred = _credential(variant, api_key)
        if not cred.get("access_token"):
            return None
        from datetime import datetime, timedelta, timezone

        from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow

        if not self.model_credits:
            try:
                self.fetch_models()
            except Exception:
                logger.debug("workbuddy[%s]: rate table unavailable for /usage", variant.id, exc_info=True)
        rates_line = _format_rates(self.model_credits)
        httpx = _httpx()
        details: list[str] = []
        windows: list[Any] = []
        raw: Any = None

        # 资源包端点对两个变体都成立（同一形状、同一套头），所以先走它。
        credit_summary: dict[str, Any] | None = None
        now = datetime.now()
        response = httpx.post(
            f"{variant.web_base}/v2/billing/meter/get-user-resource",
            headers=_billing_headers(cred, origin=variant.web_base),
            json=_resource_body(),
            timeout=HTTP_TIMEOUT,
        )
        payload = response.json() if response.content else {}
        if not isinstance(payload, dict) or payload.get("code") not in (0, None):
            logger.warning(
                "workbuddy[%s]: resource probe failed HTTP %s: %s",
                variant.id, response.status_code, str(payload)[:200],
            )
        packages = _packages_from_resource(payload)
        if packages:
            credit_summary = _credit_summary(packages, unlimited=False)
            total_remain = int(credit_summary["total_remain"])
            credit_summary["total_size"] = total_remain
            credit_summary["total_used"] = 0
            upstream_total = _number_from(
                ((payload.get("data") or {}).get("Response") or {}).get("Data") or {}, "TotalDosage",
            ) if isinstance(payload.get("data"), dict) else None
            windows.append(AccountUsageWindow(
                label="积分",
                used_percent=None,
                detail=f"总分 {total_remain} 分",
            ))
            if upstream_total is not None and int(upstream_total) != total_remain:
                logger.info(
                    "workbuddy[%s]: 本地合计 %s 与上游 TotalDosage %s 不一致（口径待核）",
                    variant.id, total_remain, int(upstream_total),
                )
            details.extend(_credit_lines(credit_summary))

        # 企业号另有一条「周期额度」读法（个人号对它一律 400 uid or enterpriseID is empty）。
        record = _desktop_record(variant)
        enterprise_id = str(record.get("enterprise_id") or "").strip()
        if variant.region == "cn" and enterprise_id:
            enterprise = httpx.post(
                f"{variant.web_base}/v2/billing/meter/get-enterprise-user-usage",
                headers=_billing_headers(cred, origin=variant.web_base),
                json={},
                timeout=HTTP_TIMEOUT,
            )
            enterprise_json = enterprise.json() if enterprise.content else {}
            enterprise_data = enterprise_json.get("data") if isinstance(enterprise_json, dict) else None
            if isinstance(enterprise_data, dict):
                limit = enterprise_data.get("limitNum")
                if not isinstance(limit, int):
                    limit = enterprise_data.get("limit_num")
                used_value = enterprise_data.get("credit")
                if not isinstance(used_value, (int, float)):
                    used_value = enterprise_data.get("used_num")
                cycle_end = str(enterprise_data.get("cycleEndTime") or "")
                reset = str(enterprise_data.get("cycleResetTime") or "")
                if limit == -1:
                    windows.append(AccountUsageWindow(
                        label="企业额度",
                        used_percent=None,
                        detail="不限量（企业版）" + (f" · 本周期至 {cycle_end}" if cycle_end else ""),
                    ))
                    details.append("企业账号：额度不限量" + (f"（本周期至 {cycle_end}）" if cycle_end else ""))
                elif isinstance(limit, (int, float)) and limit > 0:
                    used_float = float(used_value or 0)
                    windows.append(AccountUsageWindow(
                        label="企业额度",
                        used_percent=round(used_float / float(limit) * 100, 1),
                        detail=f"剩余 {max(0.0, float(limit) - used_float):g} / {float(limit):g} 分"
                               + (f" · 本周期至 {cycle_end}" if cycle_end else ""),
                    ))
                if reset:
                    details.append(f"企业周期重置：{reset}")

        if not windows:
            details.append("上游账单接口这次没给我们额度（请求头必须成套，缺一个就 403；也可能是网络问题）。")

        try:
            if credit_summary is not None:
                self.last_credits = dict(credit_summary)
        except Exception:
            pass
        account = _desktop_record(variant)
        who = account.get("nickname") or account.get("display_name") or "—"
        account_line = f"账号：{who}"
        if account.get("enterprise_name"):
            account_line += f"（{account['enterprise_name']}）"
        if account.get("uid"):
            account_line += f" · uid {account['uid']}"
        if account.get("expires_at_ms"):
            account_line += " · 令牌到期 " + time.strftime("%Y-%m-%d %H:%M", time.localtime(account["expires_at_ms"] / 1000))
        details.append(account_line)
        if rates_line:
            details.append(rates_line)
        return AccountUsageSnapshot(
            provider=self.name,
            source=f"{variant.id}_billing",
            fetched_at=datetime.now(timezone.utc),
            title=f"{variant.label} 额度",
            windows=tuple(windows),
            details=tuple(details),
            identity=who if who != "—" else None,
            raw=raw if isinstance(raw, dict) else None,
        )


def _origin(base_url: str) -> str:
    """``https://www.workbuddy.ai/v2`` → ``https://www.workbuddy.ai``（目录/计费都挂在这）。"""
    parsed = urlparse(base_url or "")
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return ""


class CNWorkBuddyProfile(WorkBuddyProfile):
    """国内版（``workbuddy-desktop.info`` · copilot.tencent.com）。"""

    variant = CN


class GlobalWorkBuddyProfile(WorkBuddyProfile):
    """国际版（``workbuddy-desktop-ai.info`` · www.workbuddy.ai）。"""

    variant = GLOBAL


def _build_profile(variant: Variant, profile_cls: type[WorkBuddyProfile]) -> WorkBuddyProfile:
    return profile_cls(
        name=variant.id,
        # 不用别名：别名会被镜像成 PROVIDER_REGISTRY 的独立行，列表里会出现重复项。
        aliases=(),
        display_name=variant.label,
        description=(
            "WorkBuddy（国内版）— 本机桌面登录，企业账号额度"
            if variant.region == "cn"
            else "WorkBuddy AI（国际版）— 本机桌面登录，消耗账号自己的积分"
        ),
        env_vars=(_token_env_name(variant),),
        base_url=f"{variant.chat_base}/v2",
        auth_type="api_key",
        default_headers=_headers(variant, _desktop_record(variant)),
        default_aux_model=variant.aux_model,
        fallback_models=variant.fallback_models,
        supports_model_listing=True,
        supports_vision=True,
        auth_handler=_make_auth_handler(variant),
        refresh_credential=_make_refresh(variant),
    )


PROFILES = (
    _build_profile(CN, CNWorkBuddyProfile),
    _build_profile(GLOBAL, GlobalWorkBuddyProfile),
)

for _profile in PROFILES:
    register_provider(_profile)


# ── /workbuddy 会话命令 + CLI（原 workbuddy-models 插件，已并入统一包）──
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
    """注册会话内斜杠命令与同名 CLI 子命令（统一包的 agent 半边）。"""
    ctx.register_command(
        "workbuddy",
        _handle_command,
        description="WorkBuddy 两类账户的账号、模型、积分倍率与额度",
        args_hint="[cn|ai|account|models|usage]",
    )
    try:
        ctx.register_cli_command(
            "workbuddy",
            "列出 WorkBuddy（国内版/国际版）的模型、倍率与额度",
            _setup_cli,
            _handle_cli,
        )
    except Exception:
        logger.debug("hermes-workbuddy: CLI command registration skipped", exc_info=True)
