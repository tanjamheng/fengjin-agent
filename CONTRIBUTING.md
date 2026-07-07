# 贡献指南

欢迎为风堇 Agent 贡献代码、文档或反馈！

## 提交 Issue

遇到 Bug、有功能建议、或发现风堇的回复不够还原？请提交 [Issue](https://github.com/tanjamheng/ai-fengjin/issues)。

## 提交 Pull Request

1. Fork 本仓库
2. 创建你的分支 (`git checkout -b feat/your-feature`)
3. 提交你的改动 (`git commit -m 'feat: 描述你的改动'`)
4. 推送到你的分支 (`git push origin feat/your-feature`)
5. 创建 Pull Request

## 开发规范

- Python 后端代码遵循 `核心文档/核心3_开发规范.md` 中的红线
- 前端代码遵循 TypeScript strict 模式，不引入 React/Vue/CSS 框架
- 新增依赖需经讨论确认
- 配置文件有默认回退，不阻塞启动
- 所有持有 GPU/ChromaDB 资源的模块必须有幂等的 `cleanup()` 方法

## 参考文档

- `CLAUDE.md` — 架构速查、红线、文件结构
- `核心文档/` — 需求、架构、规范、协议的完整文档
