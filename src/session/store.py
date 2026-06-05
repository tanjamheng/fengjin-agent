"""会话持久化：单文件原子读写"""

import json
import os
from pathlib import Path
from typing import Optional

from .session import Session
from ..utils.logger import get_logger


class SessionStore:
    """会话文件存储

    只负责单个 JSON 文件的原子读写，不涉及业务逻辑。
    文件名 = {session_id}.json，title 只存在 JSON 内部。
    """

    def __init__(self, data_dir: str = "data/sessions"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log = get_logger("session_store")
        self._cleanup_temp_files()

    def save_session(self, session: Session) -> None:
        """原子写入单个会话文件

        用临时文件 + os.replace 保证写入原子性，
        进程崩溃不会导致文件损坏。
        """
        path = self._session_path(session.session_id)
        tmp_path = str(path) + ".tmp"

        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(session.model_dump(), f, ensure_ascii=False, indent=2, default=str)

        os.replace(tmp_path, path)
        self.log.debug(f"会话已保存: {session.session_id}")

    def load_session(self, session_id: str) -> Optional[Session]:
        """读取单个会话文件"""
        path = self._session_path(session_id)
        if not path.exists():
            return None

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return Session(**data)
        except (json.JSONDecodeError, Exception) as e:
            self.log.error(f"会话文件损坏: {session_id}, 错误: {e}")
            return None

    def list_session_files(self) -> list[Path]:
        """扫描目录，返回所有会话文件路径"""
        return sorted(
            self.data_dir.glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

    def delete_session(self, session_id: str) -> bool:
        """删除单个会话文件"""
        path = self._session_path(session_id)
        if path.exists():
            path.unlink()
            self.log.info(f"会话已删除: {session_id}")
            return True
        return False

    def _session_path(self, session_id: str) -> Path:
        return self.data_dir / f"{session_id}.json"

    def _cleanup_temp_files(self) -> None:
        """清理崩溃残留的 .tmp 临时文件"""
        for tmp_file in self.data_dir.glob("*.json.tmp"):
            try:
                tmp_file.unlink()
                self.log.debug(f"已清理残留临时文件: {tmp_file.name}")
            except OSError as e:
                self.log.warning(f"清理临时文件失败: {tmp_file.name}, {e}")
