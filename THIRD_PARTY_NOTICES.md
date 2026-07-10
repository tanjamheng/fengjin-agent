# 第三方依赖声明

本项目使用了下列第三方开源软件依赖。仓库根目录的 `LICENSE` 仅适用于本项目原创代码与原创文档；第三方依赖仍分别遵循其原始许可证。具体版本以 `requirements.txt`、`frontend/package.json` 和 `frontend/package-lock.json` 为准。

## Python 运行依赖

| 依赖 | 用途 | 常见许可证 |
|---|---|---|
| fastapi | 后端 WebSocket / HTTP 服务 | MIT |
| uvicorn | ASGI 服务运行 | BSD-3-Clause |
| openai | OpenAI 兼容 API 客户端 | Apache-2.0 |
| pydantic | 配置与协议数据校验 | MIT |
| pyyaml | YAML 配置解析 | MIT |
| python-dotenv | `.env` 环境变量加载 | BSD-3-Clause |
| loguru | 日志系统 | MIT |
| rank-bm25 | BM25 稀疏检索 | Apache-2.0 |
| rich | CLI 输出渲染 | MIT |
| chromadb | 向量数据库 | Apache-2.0 |
| sentence-transformers | 嵌入模型加载与推理 | Apache-2.0 |
| langchain-text-splitters | 可选文本切分器 | MIT |
| pypdf | PDF 文档读取 | BSD-3-Clause |
| python-docx | Word 文档读取 | MIT |
| torch | 深度学习运行时 | BSD-style |
| transformers | Transformer 模型加载 | Apache-2.0 |
| accelerate | 模型推理辅助 | Apache-2.0 |
| modelscope | 模型下载 | Apache-2.0 |

## 前端 / Electron 依赖

| 依赖 | 用途 | 常见许可证 |
|---|---|---|
| electron | Windows 桌面客户端运行时 | MIT |
| electron-builder | Electron 打包 | MIT |
| electron-vite | Electron + Vite 构建 | MIT |
| typescript | TypeScript 编译 | Apache-2.0 |
| 7zip-bin | electron-builder 打包辅助；其中 7-Zip 二进制遵循其上游许可证 | MIT / 上游 7-Zip 许可证 |

## 传递依赖

上述依赖会引入各自的传递依赖。传递依赖的完整列表、版本和许可证以包管理器锁定文件及依赖包内自带的 `LICENSE` / `NOTICE` 文件为准：

- Python 依赖：`requirements.txt`
- 前端 / Electron 依赖：`frontend/package-lock.json`

若你继续分发、修改或二次打包本项目，请同时遵守所有直接依赖和传递依赖的许可证要求。

另见：

- `THIRD_PARTY_ASSETS.md`：角色、图片、模型、剧情文本、语音文本、RAG 知识库资料等非代码素材声明
- `LICENSE`：项目原创代码与原创文档的 MIT License
