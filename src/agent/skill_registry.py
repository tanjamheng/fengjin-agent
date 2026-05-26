"""Skill 注册中心

管理所有 Skill 的注册、发现和获取。
"""

from typing import Dict, List, Optional, Type
from ..capabilities.skill import SkillBase, SkillMeta
from ..utils.logger import get_logger, generate_trace_id


class SkillRegistry:
    """Skill 注册中心"""

    _instance: Optional["SkillRegistry"] = None
    _skills: Dict[str, SkillBase] = {}

    def __new__(cls) -> "SkillRegistry":
        """单例模式"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._skills = {}
        return cls._instance

    def register(self, skill: SkillBase) -> None:
        """注册 Skill"""
        trace_id = generate_trace_id()
        log = get_logger(trace_id)

        name = skill.meta.name
        if name in self._skills:
            log.warning(f"Skill {name} 已存在，将被覆盖")

        self._skills[name] = skill
        log.info(f"注册 Skill: {name} v{skill.meta.version}")

    def register_class(self, skill_class: Type[SkillBase], **kwargs) -> None:
        """通过类注册 Skill"""
        skill = skill_class(**kwargs)
        self.register(skill)

    def unregister(self, name: str) -> bool:
        """注销 Skill"""
        if name in self._skills:
            skill = self._skills[name]
            skill.cleanup()
            del self._skills[name]
            return True
        return False

    def get(self, name: str) -> Optional[SkillBase]:
        """获取指定 Skill"""
        return self._skills.get(name)

    def list_skills(self) -> List[SkillMeta]:
        """列出所有已注册 Skill 的元信息"""
        return [skill.meta for skill in self._skills.values()]

    def list_names(self) -> List[str]:
        """列出所有 Skill 名称"""
        return list(self._skills.keys())

    def initialize_all(self) -> None:
        """初始化所有 Skill"""
        trace_id = generate_trace_id()
        log = get_logger(trace_id)

        for name, skill in self._skills.items():
            try:
                skill.initialize()
                log.info(f"初始化 Skill: {name}")
            except Exception as e:
                log.error(f"初始化 Skill {name} 失败: {e}")

    def cleanup_all(self) -> None:
        """清理所有 Skill"""
        trace_id = generate_trace_id()
        log = get_logger(trace_id)

        for name, skill in self._skills.items():
            try:
                skill.cleanup()
                log.info(f"清理 Skill: {name}")
            except Exception as e:
                log.error(f"清理 Skill {name} 失败: {e}")

    def execute(self, name: str, context) -> "SkillResult":
        """执行指定 Skill"""
        from ..capabilities.skill import SkillResult

        trace_id = generate_trace_id()
        log = get_logger(trace_id)

        skill = self.get(name)
        if skill is None:
            log.error(f"Skill {name} 不存在")
            return SkillResult(
                success=False,
                error=f"Skill {name} not found"
            )

        if not skill.is_initialized():
            try:
                skill.initialize()
            except Exception as e:
                log.error(f"Skill {name} 初始化失败: {e}")
                return SkillResult(
                    success=False,
                    error=f"Skill initialization failed: {e}"
                )

        log.info(f"执行 Skill: {name}")
        try:
            result = skill.execute(context)
            log.info(f"Skill {name} 执行完成: success={result.success}")
            return result
        except Exception as e:
            log.error(f"Skill {name} 执行失败: {e}")
            return SkillResult(
                success=False,
                error=str(e)
            )

    def clear(self) -> None:
        """清空所有 Skill"""
        self.cleanup_all()
        self._skills.clear()

    @property
    def count(self) -> int:
        """返回已注册 Skill 数量"""
        return len(self._skills)


def get_registry() -> SkillRegistry:
    """获取全局 SkillRegistry 单例"""
    return SkillRegistry()