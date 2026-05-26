"""Markdown 分块策略

按标题层级（# ## ###）分块，保留文档结构。
超长章节自动用递归分块二次切分。
"""

import re
from typing import List, Optional
from .base import SplitterStrategy, TextChunk


class MarkdownSplitter(SplitterStrategy):
    """Markdown 标题分块

    两级切分：
    1. 按指定标题层级（默认 ## 和 ###）切分，保留文档结构
    2. 超过 max_chunk_size 的块用递归分块二次切分
    """

    def __init__(
        self,
        headers_to_split_on: Optional[List[str]] = None,
        strip_headers: bool = False,
        max_chunk_size: int = 1500,
        chunk_overlap: int = 100,
        separators: Optional[List[str]] = None
    ):
        self.headers_to_split_on = headers_to_split_on or ["##", "###"]
        self.strip_headers = strip_headers
        self.max_chunk_size = max_chunk_size
        self.chunk_overlap = chunk_overlap
        self.separators = separators or ["\n\n", "\n", "。", "！", "？", "；", ".", "!", "?", ";", " ", ""]

    def split(self, text: str, metadata: dict = None) -> List[TextChunk]:
        """切分 Markdown 文本"""
        if not text:
            return []

        base_metadata = metadata or {}

        # 第一级：按标题切分
        sections = self._split_by_headers(text)

        # 第二级：对超长块递归切分
        chunks = []
        chunk_id = 0
        for section in sections:
            header_path = section["header_path"]
            content = section["content"]

            if len(content) <= self.max_chunk_size:
                chunks.append(TextChunk(
                    content=content,
                    metadata={**base_metadata, **header_path, "chunk_index": chunk_id, "strategy": "markdown"},
                    chunk_id=chunk_id
                ))
                chunk_id += 1
            else:
                sub_chunks = self._recursive_split(content)
                for j, sub_text in enumerate(sub_chunks):
                    chunks.append(TextChunk(
                        content=sub_text,
                        metadata={
                            **base_metadata, **header_path,
                            "chunk_index": chunk_id,
                            "sub_chunk": j,
                            "strategy": "markdown"
                        },
                        chunk_id=chunk_id
                    ))
                    chunk_id += 1

        return chunks

    def _split_by_headers(self, text: str) -> List[dict]:
        """按标题层级切分文档，返回 [{header_path, content}, ...]"""
        # 构建各标题层级的正则
        header_levels = {}
        for h in self.headers_to_split_on:
            level = len(h)
            header_levels[level] = h

        lines = text.split('\n')
        sections = []

        # 标题栈：追踪当前路径（如 h1 > h2 > h3）
        header_stack = {}
        current_lines = []

        for line in lines:
            matched_level = self._match_header(line, header_levels)

            if matched_level:
                # 保存之前的块
                if current_lines:
                    content = "\n".join(current_lines).strip()
                    if content:
                        sections.append({
                            "header_path": {f"h{k}": v for k, v in header_stack.items()},
                            "content": content
                        })

                # 更新标题栈：清除同级及更深层级
                header_stack = {k: v for k, v in header_stack.items() if k < matched_level}
                header_stack[matched_level] = line.strip().lstrip('#').strip()
                current_lines = [] if self.strip_headers else [line]
            else:
                current_lines.append(line)

        # 保存最后一个块
        if current_lines:
            content = "\n".join(current_lines).strip()
            if content:
                sections.append({
                    "header_path": {f"h{k}": v for k, v in header_stack.items()},
                    "content": content
                })

        return sections

    def _match_header(self, line: str, header_levels: dict) -> Optional[int]:
        """检查行是否匹配某个标题层级，返回层级数字或 None"""
        stripped = line.strip()
        for level, prefix in header_levels.items():
            if re.match(r'^' + re.escape(prefix) + r'\s+\S', stripped):
                return level
        return None

    def _recursive_split(self, text: str) -> List[str]:
        """递归切分超长文本"""
        if len(text) <= self.max_chunk_size:
            return [text]

        best_split = -1
        for sep in self.separators:
            if sep == "":
                continue
            search_region = text[:self.max_chunk_size + 200]
            last_sep = search_region.rfind(sep)
            if last_sep > 0 and last_sep < self.max_chunk_size:
                best_split = last_sep + len(sep)
                break

        if best_split <= 0:
            best_split = self.max_chunk_size

        first_chunk = text[:best_split]
        remaining = text[best_split:]
        other_chunks = self._recursive_split(remaining)
        return [first_chunk] + other_chunks