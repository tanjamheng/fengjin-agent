"""语义分块策略

基于 embedding 相似度进行分块，相似度低于阈值时断开。
"""

from typing import List
from .base import SplitterStrategy, TextChunk


class SemanticSplitter(SplitterStrategy):
    """语义分块

    核心算法：
    1. 将文本按句子切分
    2. 计算每个句子的 embedding
    3. 计算相邻句子的 cosine similarity
    4. 相似度 < threshold 时断开为新块
    """

    def __init__(
        self,
        threshold: float = 0.85,
        min_chunk_size: int = 100,
        max_chunk_size: int = 1000,
        embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    ):
        self.threshold = threshold
        self.min_chunk_size = min_chunk_size
        self.max_chunk_size = max_chunk_size
        from ....utils.logger import get_logger
        self.log = get_logger("semantic_splitter")
        self.embedding_model_name = embedding_model
        self._embedding_model = None
        self._embedding_is_shared = False  # 是否通过 registry 共享

    def cleanup(self) -> None:
        """释放 embedding 模型（共享模型走 registry release，独立实例自主清理）"""
        if self._embedding_model is not None:
            try:
                if self._embedding_is_shared:
                    from ...embedding_registry import release
                    release()
                else:
                    # 独立实例：直接删除 + 清理 GPU 缓存
                    del self._embedding_model
                    import torch
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
            except Exception as e:
                self.log.warning("嵌入模型释放异常: {}", e)
            self._embedding_model = None
            self._embedding_is_shared = False

    def _get_embedding_model(self):
        """延迟加载 embedding 模型（优先共享注册表，路径不匹配时创建独立实例）"""
        if self._embedding_model is None:
            try:
                from pathlib import Path
                from ....utils.helpers import get_project_root, resolve_device
                from ... import embedding_registry as _reg
                model_path = self.embedding_model_name
                if not Path(model_path).is_absolute():
                    model_path = str(get_project_root() / model_path)
                self._embedding_model = _reg.acquire(model_path, resolve_device("cpu"))
                # 判断是否为共享实例：通过模块属性动态读取（避免按值捕获）
                self._embedding_is_shared = (
                    _reg._model is not None
                    and _reg._model_path is not None
                    and str(model_path) == str(_reg._model_path)
                )
            except ImportError:
                raise ImportError("请安装 sentence-transformers: pip install sentence-transformers")
        return self._embedding_model

    def _split_into_sentences(self, text: str) -> List[str]:
        """将文本切分为句子"""
        # 简化版句子切分
        import re
        # 匹配中英文句子分隔符
        sentences = re.split(r'(?<=[。！？.!?])\s*', text)
        # 过滤空句子
        return [s.strip() for s in sentences if s.strip()]

    def _cosine_similarity(self, vec1, vec2) -> float:
        """计算 cosine similarity"""
        import numpy as np
        dot_product = np.dot(vec1, vec2)
        norm1 = np.linalg.norm(vec1)
        norm2 = np.linalg.norm(vec2)
        if norm1 == 0 or norm2 == 0:
            return 0.0
        return dot_product / (norm1 * norm2)

    def split(self, text: str, metadata: dict = None) -> List[TextChunk]:
        """切分文本"""
        if not text:
            return []

        # 切分句子
        sentences = self._split_into_sentences(text)
        if len(sentences) <= 1:
            return [TextChunk(
                content=text,
                metadata={**(metadata or {}), "strategy": "semantic"},
                chunk_id=0
            )]

        # 计算句子 embedding
        model = self._get_embedding_model()
        import torch
        with torch.no_grad():
            embeddings = model.encode(sentences, convert_to_numpy=True)

        # 计算相邻句子相似度
        similarities = []
        for i in range(len(embeddings) - 1):
            sim = self._cosine_similarity(embeddings[i], embeddings[i + 1])
            similarities.append(sim)

        # 根据相似度分块
        chunks = []
        current_chunk_sentences = [sentences[0]]
        chunk_id = 0
        base_metadata = metadata or {}

        for i, sim in enumerate(similarities):
            current_sentence = sentences[i + 1]
            current_chunk_text = " ".join(current_chunk_sentences)

            # 判断是否断开
            should_split = False

            # 相似度低于阈值
            if sim < self.threshold:
                should_split = True

            # 当前块超过最大长度
            if len(current_chunk_text) > self.max_chunk_size:
                should_split = True

            # 判断是否需要合并（块太小）
            if should_split and len(current_chunk_text) < self.min_chunk_size:
                should_split = False

            if should_split:
                # 保存当前块
                chunks.append(TextChunk(
                    content=current_chunk_text,
                    metadata={**base_metadata, "chunk_index": chunk_id, "strategy": "semantic"},
                    chunk_id=chunk_id
                ))
                chunk_id += 1
                current_chunk_sentences = [current_sentence]
            else:
                current_chunk_sentences.append(current_sentence)

        # 保存最后一个块
        if current_chunk_sentences:
            final_text = " ".join(current_chunk_sentences)
            chunks.append(TextChunk(
                content=final_text,
                metadata={**base_metadata, "chunk_index": chunk_id, "strategy": "semantic"},
                chunk_id=chunk_id
            ))

        return chunks