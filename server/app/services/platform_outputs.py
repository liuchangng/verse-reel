"""platform_outputs 数据契约工具。

把 tasks.platform_outputs 从「平台 → URL 字符串」的旧弱契约，升级为
「平台 → 产物对象」的一等模型。产物对象形如：

    {
      "douyin": {"url": "...", "ratio": "9:16", "duration": 32, "status": "ok"},
      "bilibili": {"ratio": "16:9", "status": "failed", "error": "烧录失败"},
    }

历史数据可能仍是 `{platform: "url"}`（纯 URL 值），本模块的解析函数会做
兼容归一：纯字符串值自动包一层 `{"url": <str>, "status": "ok"}`，保证前端
消费方不必关心新旧两种形态。
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

OK = "ok"
FAILED = "failed"


def normalize_entry(platform: str, value) -> dict | None:
    """把单个平台 entry 归一化为产物对象。

    - value 是 str（旧契约）→ {"url": value, "status": "ok"}
    - value 是 dict → 原样补全 status 缺省值，失败时保留 error
    - 其它类型 → 返回 None（调用方决定丢弃或标记失败）
    """
    if value is None:
        return None
    if isinstance(value, str):
        url = value.strip()
        if not url:
            return None
        return {"url": url, "status": OK}
    if isinstance(value, dict):
        entry = dict(value)
        entry.setdefault("status", OK)
        # 成功 entry 不应携带 error；失败 entry 保留
        if entry.get("status") == OK and "error" in entry:
            entry.pop("error", None)
        return entry
    logger.warning("platform_outputs[%s] 值类型异常: %s", platform, type(value))
    return None


def parse_platform_outputs(raw: str | None) -> dict:
    """解析 tasks.platform_outputs 原始 JSON 字符串为 dict。

    返回 key 为平台名、value 为归一化产物对象。
    非法 JSON / 非 object / 解析失败 → 返回 {}（并记录 warning，不抛异常）。
    """
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning("platform_outputs 解析失败（视为空映射）: %s", e)
        return {}
    if not isinstance(data, dict):
        logger.warning("platform_outputs 非 JSON object，视为空映射")
        return {}
    out: dict = {}
    for plat, value in data.items():
        entry = normalize_entry(plat, value)
        if entry is not None:
            out[plat] = entry
    return out


def serialize_platform_outputs(mapping: dict) -> str | None:
    """把 dict[str, dict] 序列化为 JSON 字符串用于落库。

    空 dict → None（与「单平台任务可为空」约定一致）。
    """
    if not mapping:
        return None
    return json.dumps(mapping, ensure_ascii=False)


def ok_platforms(mapping: dict) -> list[str]:
    """返回 mapping 中 status=ok 且 url 非空的平台列表（按原顺序）。"""
    result = []
    for plat, entry in (mapping or {}).items():
        if isinstance(entry, dict) and entry.get("status") == OK and entry.get("url"):
            result.append(plat)
    return result


def failed_platforms(mapping: dict) -> dict[str, str]:
    """返回 {platform: error} 仅含失败平台。"""
    result = {}
    for plat, entry in (mapping or {}).items():
        if isinstance(entry, dict) and entry.get("status") == FAILED:
            result[plat] = entry.get("error") or "未知原因"
    return result


def primary_video_url(mapping: dict, primary_platform: str) -> str | None:
    """取主平台成片 URL；主平台失败时回退到第一个成功平台（保证旧消费方不崩）。"""
    for plat in [primary_platform, *mapping.keys()]:
        entry = mapping.get(plat)
        if isinstance(entry, dict) and entry.get("status") == OK and entry.get("url"):
            return entry["url"]
    return None
