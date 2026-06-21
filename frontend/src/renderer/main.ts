/**
 * Renderer 入口 — 串联五大模块 + 状态管理
 *
 * 启动顺序：CharacterDisplay → WSClient → ChatUI → HistorySidebar
 * 通过回调将模块连接，模块间不直接互相引用。
 */

import { appState } from "./state";
import { CharacterDisplay } from "./modules/character/CharacterDisplay";
import { WSClient } from "./modules/ws/WSClient";
import { ChatUI } from "./modules/chat/ChatUI";
import { HistorySidebar } from "./modules/sidebar/HistorySidebar";
import type { SessionMeta, ChatMessage } from "./types/protocol";

// ===== 初始化 =====

// 1. CharacterDisplay（左侧角色展示）
const charContainer = document.getElementById("character-container")!;
const character = new CharacterDisplay(charContainer);
character.onLoadComplete = () => {
  appState.isModelLoaded = true;
};
character.onLoadError = () => {
  // 渐变背景兜底，不影响聊天
  appState.isModelLoaded = true;
};
character.loadImage("./assets/fengjin.jpg");

// 2. WSClient
const ws = new WSClient();

// WS 回调 → ChatUI
ws.onStreamChunk = (text) => chat.appendAIStreamChunk(text);
ws.onStreamEnd = (fullText, _action) => {
  chat.finalizeAIMessage(fullText);
  appState.isReplying = false;
  sidebar.setDisabled(false);
};
ws.onBlocked = (message) => {
  chat.endReplyMode(); // 丢弃未固化的流式气泡 + 解锁输入
  chat.appendSystemMessage(message, "blocked");
  appState.isReplying = false;
  sidebar.setDisabled(false);
};
ws.onThinking = () => chat.showThinking();
ws.onError = (message) => {
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
  appState.currentSessionId = sessionId;
  sidebar.setActive(sessionId);
  chat.clearMessages();
  chat.loadMessages(messages);
};

ws.onSessionDeleted = (sessionId: string) => {
  appState.sessions = appState.sessions.filter((s) => s.id !== sessionId);
  sidebar.renderList(appState.sessions);
  if (appState.currentSessionId === sessionId) {
    appState.currentSessionId = "";
    chat.clearMessages();
    sidebar.setActive("");
  }
};

ws.onQuickReplies = (replies: string[]) => chat.showQuickReplies(replies);

ws.onConnected = (sessionId: string) => {
  appState.currentSessionId = sessionId;
  ws.listSessions();
};

ws.onStatusChange = (status) => {
  appState.wsStatus = status;
  chat.updateConnectionStatus(status);
};

// 3. ChatUI（中间对话区）
const chatArea = document.getElementById("chat-area")!;
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
const sidebarContainer = document.getElementById("sidebar-container")!;
const sidebar = new HistorySidebar(sidebarContainer);
sidebar.onNewChat = () => {
  appState.currentSessionId = "";
  ws.resetSessionId(); // 重置 WS 客户端 session_id，下条消息发空字符串
  sidebar.setActive("");
  chat.clearMessages();
};
sidebar.onSelectSession = (sessionId: string) => {
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

// ===== 连接 =====
ws.connect("ws://127.0.0.1:8765/ws");

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

// ===== 重连按钮 =====
document.querySelector(".status-reconnect-btn")?.addEventListener("click", () => {
  ws.disconnect();
  ws.connect("ws://127.0.0.1:8765/ws");
});
