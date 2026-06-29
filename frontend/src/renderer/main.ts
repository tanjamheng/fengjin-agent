/**
 * Renderer 入口 — 串联五大模块 + 状态管理
 *
 * 启动顺序：CharacterDisplay → WSClient → ChatUI → HistorySidebar
 * 通过回调将模块连接，模块间不直接互相引用。
 */

import { CONFIG } from "./config";
import { appState } from "./state";
import { CharacterDisplay } from "./modules/character/CharacterDisplay";
import { WSClient } from "./modules/ws/WSClient";
import { ChatUI } from "./modules/chat/ChatUI";
import { HistorySidebar } from "./modules/sidebar/HistorySidebar";
import { SettingsPanel, type SettingsData } from "./modules/settings/SettingsPanel";
import type { SessionMeta, ChatMessage } from "./types/protocol";

// ===== 会话加载保护 =====
let _loadingSession = false;
let _loadingSessionId: string | null = null; // 跟踪正在加载的会话ID，防止快速切换覆盖
let _loadTimer: ReturnType<typeof setTimeout> | null = null;

// ===== 初始化 =====

// 1. CharacterDisplay（左侧角色展示）
const charContainer = document.getElementById("character-container");
if (!charContainer) throw new Error("Missing #character-container");
const character = new CharacterDisplay(charContainer);
character.onLoadComplete = () => {
  appState.isModelLoaded = true;
};
character.onLoadError = () => {
  // 渐变背景兜底，不影响聊天
  appState.isModelLoaded = true;
};
character.loadImage(CONFIG.character.imagePath);

// 2. WSClient
const ws = new WSClient();

// WS 回调 → ChatUI
ws.onStreamChunk = (text) => {
  if (_loadingSession) return; // 会话加载中，忽略废弃回复的流式分片
  chat.appendAIStreamChunk(text);
};
ws.onStreamEnd = (fullText, _action) => {
  if (_loadingSession) return; // 会话加载中，忽略废弃回复的 end 包
  chat.finalizeAIMessage(fullText);
  appState.isReplying = false;
  sidebar.setDisabled(false);
};
ws.onBlocked = (message) => {
  if (_loadingSession) return; // 会话加载中，忽略废弃回复的 blocked 包
  chat.endReplyMode(); // 丢弃未固化的流式气泡 + 解锁输入
  chat.appendSystemMessage(message, "blocked");
  appState.isReplying = false;
  sidebar.setDisabled(false);
};
ws.onThinking = () => {
  if (_loadingSession) return; // 会话加载中，忽略废弃思考中状态
  chat.showThinking();
};
ws.onError = (message) => {
  if (_loadingSession) {
    // 会话加载中收到错误：清除保护状态，显示真实错误而非超时提示
    if (_loadTimer !== null) { clearTimeout(_loadTimer); _loadTimer = null; }
    _loadingSession = false;
    _loadingSessionId = null;
    ws.resetSessionId(); // 加载失败重置 _sessionId
  }
  chat.endReplyMode(); // 丢弃未固化的流式气泡 + 解锁输入
  chat.appendSystemMessage(message, "warning");
  appState.isReplying = false;
  sidebar.setDisabled(false);
};

// WS 会话回调 → HistorySidebar
ws.onSessionList = (sessions: SessionMeta[]) => {
  appState.sessions = sessions;
  sidebar.renderList(sessions);
};
ws.onSessionLoaded = (sessionId: string, title: string, messages: ChatMessage[]) => {
  if (!_loadingSession || sessionId !== _loadingSessionId) return; // 非加载中或ID不匹配，忽略
  if (_loadTimer !== null) { clearTimeout(_loadTimer); _loadTimer = null; }
  _loadingSession = false;
  _loadingSessionId = null;
  ws.setSessionId(sessionId); // 由回调决定更新时机，防止废弃消息污染 _sessionId
  appState.currentSessionId = sessionId;
  appState.isReplying = false;
  sidebar.setActive(sessionId);
  sidebar.setDisabled(false);
  chat.hideQuickReplies();
  chat.clearMessages();
  chat.loadMessages(messages);
  chat.endReplyMode(); // 解锁 InputController（可能从回复中切换而来）
};

ws.onSessionDeleted = (sessionId: string) => {
  appState.sessions = appState.sessions.filter((s) => s.id !== sessionId);
  sidebar.renderList(appState.sessions);
  if (appState.currentSessionId === sessionId) {
    appState.currentSessionId = "";
    appState.isReplying = false; // 防止 isReplying 残留锁定 UI
    ws.resetSessionId(); // 当前会话被删，重置 WS 客户端 session_id
    ws.sendCancel(); // 取消进行中的回复
    chat.endReplyMode(); // 清理 InputController 状态
    chat.clearMessages();
    chat.hideQuickReplies(); // 清除残留快捷回复
    sidebar.setActive("");
    sidebar.setDisabled(false); // 恢复侧边栏交互
  }
};

ws.onQuickReplies = (replies: string[]) => chat.showQuickReplies(replies);

// 会话ID变更：首次发消息后端创建会话 / blocked / error / cancel 等场景统一处理
ws.onSessionChanged = (sessionId: string) => {
  if (!sessionId) return;
  appState.currentSessionId = sessionId;
  sidebar.setActive(sessionId);
  ws.listSessions(); // 刷新侧边栏，显示新会话条目
};

ws.onConnected = (sessionId: string) => {
  // 清除会话加载保护状态（防止跨连接残留）
  if (_loadTimer !== null) { clearTimeout(_loadTimer); _loadTimer = null; }
  _loadingSession = false;
  _loadingSessionId = null;
  appState.currentSessionId = sessionId;
  appState.isReplying = false;
  chat.clearMessages();
  chat.endReplyMode();
  chat.hideQuickReplies();
  sidebar.setDisabled(false);
  ws.listSessions();
};

// 配置回调
let _settingsData: SettingsData | null = null;
let _settingsPanelVisible = false;

ws.onCurrentConfig = (data) => {
  _settingsData = {
    main: data.main,
    memory: data.memory,
    memory_enabled: data.memory_enabled,
  };
};

ws.onConfigUpdated = (result) => {
  if (!_settingsPanelVisible) return;
  const actions = document.querySelector(".settings-actions");
  if (!actions) return;
  // 清除旧提示
  actions.querySelector(".settings-saved-hint")?.remove();
  const hint = document.createElement("span");
  hint.className = "settings-saved-hint";
  hint.style.fontSize = "12px";
  hint.style.lineHeight = "30px";
  if (result.success) {
    hint.style.color = "var(--color-status-online)";
    hint.textContent = "✓ 已保存";
  } else if (result.errors) {
    hint.style.color = "var(--color-status-offline)";
    hint.textContent = result.errors.join("; ");
  }
  actions.insertBefore(hint, actions.firstChild);
  if (result.success) setTimeout(() => hint.remove(), 3000);
};

ws.onStatusChange = (status) => {
  appState.wsStatus = status;
  chat.updateConnectionStatus(status);
  if (status === "disconnected") {
    // 断线时恢复 UI 状态，防止 isReplying 死锁
    // 同时清除会话加载保护状态，防止跨连接残留
    if (_loadTimer !== null) { clearTimeout(_loadTimer); _loadTimer = null; }
    _loadingSession = false;
    _loadingSessionId = null;
    appState.isReplying = false;
    chat.endReplyMode();
    sidebar.setDisabled(false);
  }
};

// 3. ChatUI（中间对话区）
const chatArea = document.getElementById("chat-area");
if (!chatArea) throw new Error("Missing #chat-area");
const chat = new ChatUI(chatArea);
chat.onSend = (text) => {
  if (appState.isReplying || appState.wsStatus !== "connected") return;
  chat.appendUserMessage(text);
  chat.setReplyMode(); // 锁定输入 + 显示停止按钮
  appState.isReplying = true;
  sidebar.setDisabled(true);
  ws.sendUserMessage(text);
};
chat.onStop = () => {
  ws.sendCancel();
  chat.endReplyMode(); // 解锁输入 + 丢弃流式气泡
  appState.isReplying = false;
  sidebar.setDisabled(false);
};

// 4. HistorySidebar（右侧侧边栏）
const sidebarContainer = document.getElementById("sidebar-container");
if (!sidebarContainer) throw new Error("Missing #sidebar-container");
const sidebar = new HistorySidebar(sidebarContainer);
sidebar.onNewChat = () => {
  // 清除会话加载保护状态 + 取消进行中的回复
  if (_loadTimer !== null) { clearTimeout(_loadTimer); _loadTimer = null; }
  _loadingSession = false;
  _loadingSessionId = null;
  ws.sendCancel(); // 取消进行中的回复（如有）
  chat.endReplyMode();
  chat.hideQuickReplies(); // 清除残留快捷回复
  appState.isReplying = false;
  appState.currentSessionId = "";
  ws.resetSessionId(); // 重置 WS 客户端 session_id，下条消息发空字符串
  sidebar.setActive("");
  chat.clearMessages();
};
sidebar.onSelectSession = (sessionId: string) => {
  if (_loadingSession) return; // 防止快速点击发送多个 loadSession 请求
  ws.sendCancel(); // 取消进行中的回复，防止幽灵消息渲染到新会话
  _loadingSession = true; // 防止废弃回复的事件重置状态
  _loadingSessionId = sessionId; // 跟踪正在加载的会话ID
  appState.isReplying = true; // 加载期间禁止发送
  chat.clearMessages();
  sidebar.setDisabled(true);

  // 加载超时保护（15s 无响应则恢复 UI）
  if (_loadTimer !== null) clearTimeout(_loadTimer);
  _loadTimer = setTimeout(() => {
    _loadTimer = null;
    _loadingSessionId = null;
    appState.isReplying = false;
    sidebar.setDisabled(false);
    chat.endReplyMode(); // 解锁 InputController
    chat.appendSystemMessage("加载会话超时，请重试", "warning");
    ws.resetSessionId(); // 加载失败时重置，防止消息发错会话
    appState.currentSessionId = ""; // 同步重置 AppState
    _loadingSession = false; // 最后降低守卫
  }, CONFIG.timeouts.sessionLoadTimeout);

  ws.loadSession(sessionId);
};
sidebar.onDeleteSession = (sessionId: string) => {
  ws.deleteSession(sessionId);
};
sidebar.onClearAll = () => {
  const sessions = [...appState.sessions];
  for (const s of sessions) {
    ws.deleteSession(s.id);
  }
};

sidebar.onOpenSettings = async () => {
  _settingsPanelVisible = true;
  // 先获取当前配置
  ws.getConfig();
  // 用已有数据或默认空值初始化面板
  const initial = _settingsData ?? {
    main: { api_key: "****", base_url: "", model: "" },
    memory: { api_key: "****", base_url: "", model: "" },
    memory_enabled: false,
  };
  const panel = new SettingsPanel(initial);
  // getConfig 回调会更新数据
  const origOnConfig = ws.onCurrentConfig;
  ws.onCurrentConfig = (data) => {
    _settingsData = {
      main: data.main,
      memory: data.memory,
      memory_enabled: data.memory_enabled,
    };
    panel.updateData(_settingsData);
    ws.onCurrentConfig = origOnConfig;
  };
  const result = await panel.show();
  _settingsPanelVisible = false;
  if (!result) return; // 取消

  // 构建 update payload：null = 不改
  const main = {
    api_key: result.main.api_key,
    base_url: result.main.base_url,
    model: result.main.model,
  };
  const memory = {
    api_key: result.memory.api_key,
    base_url: result.memory.base_url,
    model: result.memory.model,
  };
  ws.updateConfig(main, memory, result.memory_enabled);
};

// ===== 连接 =====
ws.connect(CONFIG.ws.url);

// ===== 标题栏按钮（IPC） =====
document.getElementById("btn-minimize")?.addEventListener("click", () => {
  window.electronAPI?.minimize();
});
document.getElementById("btn-maximize")?.addEventListener("click", () => {
  window.electronAPI?.maximize();
});
document.getElementById("btn-close")?.addEventListener("click", () => {
  window.electronAPI?.close();
});
document.getElementById("btn-pin")?.addEventListener("click", () => {
  window.electronAPI?.toggleAlwaysOnTop();
  const btn = document.getElementById("btn-pin");
  if (btn) btn.classList.toggle("titlebar__btn--active");
});

