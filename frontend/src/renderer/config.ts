/**
 * 前端配置 — 集中管理所有可调参数
 *
 * 对应后端 config/config.yaml 的设计哲学：
 * "禁止硬编码——配置、路径、常量、魔法数字必须通过配置文件或模块级常量定义"
 *
 * 业务代码通过 `import { CONFIG } from "../config"` 引用，禁止在模块内硬编码。
 */
export const CONFIG = {
  /** 角色展示 */
  character: {
    /** 角色图片路径（相对于 index.html），V2 替换为 Three.js 模型路径 */
    imagePath: "./assets/fengjin.jpg",
  },

  /** 对话头像 */
  avatar: {
    /** 风堇 AI 头像（对话区左侧） */
    fengjin: "./assets/avatar-fengjin.png",
    /** 开拓者用户头像（对话区右侧） */
    trailblazer: "./assets/avatar-trailblazer.png",
  },

  /** WebSocket 连接 */
  ws: {
    /** 后端 WebSocket 地址 */
    url: "ws://127.0.0.1:8765/ws",
  },

  /** 超时设置 (ms) */
  timeouts: {
    /** 心跳发送间隔 */
    pingInterval: 30_000,
    /** Pong 等待超时（超时后判定断线） */
    pongTimeout: 10_000,
    /** AI 回复总超时 */
    replyTimeout: 60_000,
    /** 会话加载超时（加载历史消息的最大等待时间） */
    sessionLoadTimeout: 15_000,
  },

  /** 输入框 */
  input: {
    /** 发送按钮防抖间隔 (ms)，防止停止→发送双击穿透 */
    submitDebounceMs: 200,
    /** 输入框最小高度 (px) */
    minHeight: 40,
    /** 输入框最大高度 (px)，超出后显示滚动条 */
    maxHeight: 120,
  },

  /** 对话区 */
  chat: {
    /** 判定"已滚到底部"的距离阈值 (px)，用于自动滚动逻辑 */
    autoScrollThreshold: 30,
  },
} as const;
