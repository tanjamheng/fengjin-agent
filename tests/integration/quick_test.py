"""快速测试Agent"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.config import Config
from src.agent import Agent

print("=" * 60)
print("快速测试 Agent")
print("=" * 60)

# 加载配置
config_path = Path(__file__).parent.parent / "config" / "config.yaml"
config = Config.load(str(config_path))

# 创建Agent
agent = Agent(config)

# 测试对话
print("\n测试对话...")
response = agent.chat("你好，请用一句话介绍自己")
print(f"回复: {response}")

print("\n" + "=" * 60)
print("测试完成！Agent工作正常")
print("=" * 60)
