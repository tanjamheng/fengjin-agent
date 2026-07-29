"""记忆时间字段的规范化工具。"""

from datetime import datetime


def canonical_event_date(value: object) -> str:
    """把日期或 ISO 日期时间统一为 YYYY-MM-DD，无法解析时返回空串。"""
    if not value:
        return ""
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return ""
