"""API连接诊断脚本

排查API Key和连接问题
"""

import os
from pathlib import Path
import yaml

print("=" * 60)
print("API连接诊断")
print("=" * 60)

# 1. 检查环境变量
print("\n[1] 检查环境变量")
fengjin_key = os.environ.get("FENGJIN_API_KEY", "未设置")
print(f"  FENGJIN_API_KEY: {fengjin_key[:20] if fengjin_key != '未设置' else '未设置'}...")

# 2. 检查配置文件
print("\n[2] 检查配置文件")
config_path = Path("config/config.yaml")
if config_path.exists():
    with open(config_path, "r", encoding="utf-8") as f:
        config_data = yaml.safe_load(f)

    api_config = config_data.get("api", {})
    print(f"  base_url: {api_config.get('base_url', '未配置')}")
    api_key = api_config.get('api_key', '未配置')
    print(f"  api_key: {api_key[:20] if api_key != '未配置' else '未配置'}...")
else:
    print("  配置文件不存在!")

# 3. 测试 OpenAI SDK 初始化
print("\n[3] 测试 OpenAI SDK 初始化")
try:
    from openai import OpenAI

    client = OpenAI(
        api_key=os.getenv("FENGJIN_API_KEY", api_config.get('api_key')),
        base_url=os.getenv("FENGJIN_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
    )
    print(f"  SDK初始化成功")
    print(f"  client.base_url: {client.base_url}")
except Exception as e:
    print(f"  SDK初始化失败: {e}")

# 4. 测试实际API调用
print("\n[4] 测试API调用")
try:
    from openai import OpenAI

    client = OpenAI(
        api_key=os.getenv("FENGJIN_API_KEY", api_config.get('api_key')),
        base_url=os.getenv("FENGJIN_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
    )

    response = client.chat.completions.create(
        model=os.getenv("FENGJIN_MODEL", "glm-5.1"),
        max_tokens=100,
        messages=[{"role": "user", "content": "你好"}]
    )

    print(f"  API调用成功!")
    print(f"  响应: {response.choices[0].message.content[:50]}...")

except Exception as e:
    print(f"  API调用失败: {e}")

    # 尝试直接打印更多信息
    print("\n  [详细错误信息]")
    if hasattr(e, 'response'):
        print(f"    response: {e.response}")
    if hasattr(e, '__dict__'):
        print(f"    error attrs: {e.__dict__}")

# 5. 尝试备用配置
print("\n[5] 尝试使用标准DashScope接口")
try:
    from openai import OpenAI

    alt_base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    alt_client = OpenAI(
        api_key=os.getenv("FENGJIN_API_KEY", api_config.get('api_key')),
        base_url=alt_base_url
    )

    response = alt_client.chat.completions.create(
        model="qwen-plus",
        max_tokens=100,
        messages=[{"role": "user", "content": "你好"}]
    )

    print(f"  备用接口调用成功!")
    print(f"  响应: {response.choices[0].message.content[:50]}...")

except Exception as e:
    print(f"  备用接口调用失败: {e}")

print("\n" + "=" * 60)
print("诊断完成")
print("=" * 60)
