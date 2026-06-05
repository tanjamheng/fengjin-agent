"""记忆管理器（门面类）"""

import threading

from .config import MemoryConfig, MemorySettings
from .storage import MemoryStorage
from .extractor import MemoryExtractor
from .writer import MemoryWriter
from .retriever import MemoryRetriever
from ..utils.logger import get_logger


class MemoryManager:
    """记忆系统门面

    对外暴露两个方法：
    - retrieve(user_input) -> str：检索记忆，返回注入文本
    - extract_async(user_input, assistant_message)：异步提取+写入
    """

    def __init__(self, config: MemoryConfig):
        small_client = MemorySettings.create_memo_model_client()
        model_name = MemorySettings.get_memo_model_name()

        self.storage = MemoryStorage(config)
        self.extractor = MemoryExtractor(config, small_client, model_name, self.storage)
        self.writer = MemoryWriter(config, small_client, model_name, self.storage)
        self.retriever = MemoryRetriever(config, self.storage)
        self.log = get_logger("memory_manager")

    def retrieve(self, user_input: str) -> str:
        """检索记忆，返回格式化的记忆文本（用于注入 system prompt）"""
        return self.retriever.retrieve(user_input)

    def extract_async(self, user_input: str, assistant_message: str,
                      trace_id: str = "") -> None:
        """异步提取记忆并写入（不阻塞主流程）"""
        def _worker():
            try:
                facts = self.extractor.extract(user_input, assistant_message)
                if facts:
                    self.writer.write(facts)
            except Exception as e:
                self.log.error(f"记忆提取失败 [trace={trace_id}]: {e}", exc_info=True)

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()

    def cleanup(self) -> None:
        """停止写入线程并关闭存储"""
        self.writer.stop()
        self.storage.cleanup()
