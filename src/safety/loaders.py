"""词库加载器：从 txt 加载关键词，从 yaml 加载正则"""

import re
from pathlib import Path

import yaml

from ..utils.logger import get_logger


def load_keywords(words_dir: Path, categories: dict) -> dict[str, list[str]]:
    """从 txt 文件加载关键词

    Args:
        words_dir: 词库目录路径
        categories: 类别配置字典 {category_id: CategoryConfig}

    Returns:
        {category_id: ["keyword1", "keyword2", ...]}
        文件不存在或为空的类别返回空列表
    """
    log = get_logger("safety_loader")
    keywords: dict[str, list[str]] = {}

    for cat_id in categories:
        file_path = words_dir / f"{cat_id}.txt"
        if not file_path.exists():
            log.warning(f"词库文件不存在: {file_path}，类别 {cat_id} 关键词为空")
            keywords[cat_id] = []
            continue

        words = []
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        words.append(line)
        except Exception as e:
            log.warning(f"读取词库文件失败: {file_path}, {e}")
            words = []

        keywords[cat_id] = words
        log.debug(f"加载词库 {cat_id}: {len(words)} 个关键词")

    return keywords


def load_regex_patterns(regex_path: Path) -> list[dict]:
    """从 yaml 文件加载正则规则

    Args:
        regex_path: regex_patterns.yaml 路径

    Returns:
        [{"category": str, "pattern": compiled_regex, "raw": str}, ...]
        编译失败的正则会被跳过
    """
    log = get_logger("safety_loader")

    if not regex_path.exists():
        log.warning(f"正则规则文件不存在: {regex_path}")
        return []

    try:
        with open(regex_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception as e:
        log.warning(f"读取正则规则文件失败: {regex_path}, {e}")
        return []

    raw_patterns = data.get("patterns", [])
    compiled = []

    for item in raw_patterns:
        category = item.get("category", "")
        raw = item.get("pattern", "")

        if not category or not raw:
            continue

        try:
            compiled_regex = re.compile(raw)
            compiled.append({
                "category": category,
                "pattern": compiled_regex,
                "raw": raw,
            })
        except re.error as e:
            log.warning(f"正则编译失败 [{category}]: {raw}, 错误: {e}")

    log.debug(f"加载正则规则: {len(compiled)}/{len(raw_patterns)} 条成功")
    return compiled
