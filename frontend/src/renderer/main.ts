/**
 * Renderer 入口 — 串联五大模块 + 启动器
 */

import { CONFIG } from "./config";
import { appState } from "./state";
import { CharacterDisplay } from "./modules/character/CharacterDisplay";
import { WSClient } from "./modules/ws/WSClient";
import { ChatUI } from "./modules/chat/ChatUI";
import { HistorySidebar } from "./modules/sidebar/HistorySidebar";
import { SettingsPanel, type SettingsData } from "./modules/settings/SettingsPanel";
import { LauncherRenderer } from "./modules/launcher/LauncherRenderer";
import { Logger } from "./utils/logger";
import type { SessionMeta, ChatMessage } from "./types/protocol";

const log = new Logger("Main");

// ===== 启动器 =====
let _isLauncherMode = true; // 默认加载模式

// 监听主进程：是否进入加载模式
const api = (window as any).electronAPI;
if (api) {
  api.onLauncherMode((mode: string) => {
    if (mode === "loading") _isLauncherMode = true;
  });
}

async function _resolveWsUrl(): Promise<string> {
  if (api?.getWsUrl) {
    return await api.getWsUrl();
  }
  return CONFIG.ws.browserDevelopmentUrl;
}

function _connectWs(ws: WSClient): void {
  _resolveWsUrl()
    .then((url) => ws.connect(url))
    .catch((e) => {
      log.warn("Failed to resolve secured WS URL: {}", e);
      // Electron 的实际端口只能由主进程提供；IPC 出错时不能退回固定端口。
      if (!api) ws.connect(CONFIG.ws.browserDevelopmentUrl);
    });
}

// HMR 热重载检测：后端已在运行则跳过加载页
async function _checkBackendAlive(): Promise<boolean> {
  if (api?.isBackendAlive) {
    return await api.isBackendAlive();
  }
  return false;
}

// ===== 会话加载保护 =====
let _loadingSession = false;
let _loadingSessionId: string | null = null;
let _loadTimer: ReturnType<typeof setTimeout> | null = null;

// ===== 首次配置标记 =====
let _pendingFirstTimeConfig = false;
let _ws: WSClient | null = null;

// ===== 初始化 =====

// 1. CharacterDisplay（左侧角色展示 — 始终显示）
const charContainer = document.getElementById("character-container");
if (!charContainer) throw new Error("Missing #character-container");
const character = new CharacterDisplay(charContainer);
character.onLoadComplete = () => { appState.isModelLoaded = true; };
character.onLoadError = () => { appState.isModelLoaded = true; };
character.loadImage(CONFIG.character.imagePath);

// 启动器（如果有 IPC 支持）
const launcherContainer = document.getElementById("launcher-container");
const appPanel = document.getElementById("app-panel");
let launcherRenderer: LauncherRenderer | null = null;

if (api && launcherContainer && appPanel) {
  // 立刻创建加载页 + 注册 IPC 监听——放在 _checkBackendAlive() 之前，
  // 否则 await 的 1.5s 内后端已发出大量进度消息，全部丢失
  launcherRenderer = new LauncherRenderer(launcherContainer);
  let _doneArrivedEarly = false;
  let _doneHandlerSet = false;

  api.onLauncherState((state: any) => {
    launcherRenderer?.update(state);
    // 检测 connect 步骤：LauncherManager 进入最后一步 → 启动 WS
    if (!_connectStepActive && state.phase === "system_load" && state.stepText === "正在建立连接...") {
      _startWsConnection();
    }
    if (state.phase === "done" && !_doneHandlerSet) {
      _doneArrivedEarly = true;
    }
  });

  api.onLauncherNeedConfig(() => {
    if (_isLauncherMode) {
      _pendingFirstTimeConfig = true;
    } else {
      _showAndApplyFirstTimeSettings();
    }
  });

  launcherRenderer.onDone = () => {
    _transitionToChat();
  };
  _doneHandlerSet = true;
  // 如果 done 状态在 onDone 注册前就已到达，补调一次
  if (_doneArrivedEarly) {
    _doneArrivedEarly = false;
    _transitionToChat();
  }

  // HMR 热重载：后端已在线则跳过加载页
  const backendAlive = await _checkBackendAlive();
  if (backendAlive) {
    log.info("后端已在运行，跳过加载页 (HMR)");
    _isLauncherMode = false;
    launcherContainer.style.display = "none";
    appPanel.style.display = "flex";
    appPanel.classList.add("app-panel--visible");
    initChatModules();
  }
}

/** 显示首次配置面板 → 保存 .env → 重试启动后端 → 等待就绪 → 重连 WS */
async function _showAndApplyFirstTimeSettings(): Promise<void> {
  const panel = new SettingsPanel({
    main: { api_key: "", base_url: "", model: "" },
    memory: { api_key: "", base_url: "", model: "" },
    memory_enabled: false,
  }, undefined, "保存并启动", "配置 API 才能让风堇说话哦");

  const result = await panel.show();
  if (!result) {
    // 用户取消 — 风堇不会说话，但界面可正常浏览
    log.info("First-time settings cancelled by user");
    return;
  }

  const saved = await api.settingsWriteEnv(result);
  panel.close();
  if (!saved.success) {
    const message = saved.error || "写入 .env 失败";
    log.error("Failed to write .env: {}", message);
    window.alert(message);
    return;
  }

  // 重试启动后端
  await api.launcherRetry();

  // 等待后端就绪，然后重连 WS
  await _waitForBackend();
  if (_ws) {
    _connectWs(_ws);
  }
}

/** 轮询后端健康检查，直到就绪（最长等 60s） */
async function _waitForBackend(): Promise<void> {
  const maxWait = 60_000;
  const interval = 1500;
  const start = Date.now();
  if (!api?.isBackendAlive) {
    log.warn("Backend readiness checks require Electron IPC");
    return;
  }
  while (Date.now() - start < maxWait) {
    try {
      if (await api.isBackendAlive()) {
        log.info("Backend is ready after config save");
        return;
      }
    } catch {
      // 后端未就绪，继续等
    }
    await new Promise((r) => setTimeout(r, interval));
  }
  log.warn("Backend did not become ready within 60s after config save");
}

// ===== 加载页过渡控制 =====
let _connectStepActive = false;
let _chatModulesInitialized = false;

/** connect 步骤激活 → 启动 WS 连接（只执行一次） */
function _startWsConnection(): void {
  if (_chatModulesInitialized) return;
  _chatModulesInitialized = true;
  _connectStepActive = true;
  initChatModules();
  // 5 秒超时兜底：WS 始终连不上也通知完成，防止卡加载页
  setTimeout(() => {
    if (_connectStepActive) {
      _connectStepActive = false;
      api?.launcherCompleteConnect();
    }
  }, 5000);
}

/** "done" 到达 → 加载页淡出，显示聊天界面 */
function _transitionToChat(): void {
  if (!launcherContainer || !appPanel) return;
  launcherContainer.style.opacity = "0";
  setTimeout(() => {
    launcherContainer!.style.display = "none";
    appPanel!.style.display = "flex";
    requestAnimationFrame(() => {
      appPanel!.classList.add("app-panel--visible");
    });
    _isLauncherMode = false;
    if (_pendingFirstTimeConfig) {
      _pendingFirstTimeConfig = false;
      _showAndApplyFirstTimeSettings();
    }
  }, 300);
}

// 2. 如果是非 Electron 环境（浏览器 dev），直接显示聊天
if (!api) {
  _isLauncherMode = false;
  if (launcherContainer) launcherContainer.style.display = "none";
  if (appPanel) {
    appPanel.style.display = "flex";
    appPanel.classList.add("app-panel--visible");
  }
}

// ===== 聊天模块（延迟到加载完成后初始化） =====
function initChatModules(): void {
  // 2. WSClient
  const ws = new WSClient();
  _ws = ws;

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
  ws.listSessions();
};
ws.onBlocked = (message) => {
  if (_loadingSession) return; // 会话加载中，忽略废弃回复的 blocked 包
  chat.endReplyMode(); // 丢弃未固化的流式气泡 + 解锁输入
  chat.appendSystemMessage(message, "blocked");
  appState.isReplying = false;
  sidebar.setDisabled(false);
  ws.listSessions();
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
  // connect 步骤中出错也通知 LauncherManager 完成，防止卡加载页
  if (_connectStepActive) { _connectStepActive = false; api?.launcherCompleteConnect(); }
  if (ws.status === "connected") {
    ws.listSessions();
  }
};

// WS 会话回调 → HistorySidebar
ws.onSessionList = (sessions: SessionMeta[]) => {
  appState.sessions = sessions;
  sidebar.renderList(sessions);
  // connect 步骤中会话列表到达 → 通知 LauncherManager 完成
  if (_connectStepActive) { _connectStepActive = false; api?.launcherCompleteConnect(); }
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
  log.info("Session deleted (id={})", sessionId);
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

ws.onSessionRenamed = (sessionId: string, title: string) => {
  log.info("Session renamed (id={}, title={})", sessionId, title);
  appState.sessions = appState.sessions.map((s) =>
    s.id === sessionId ? { ...s, title } : s
  );
  sidebar.renderList(appState.sessions);
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
  _settingsPanelClose?.(); _settingsPanelClose = null; // 重连时清理残留设置面板句柄
  _settingsPanelVisible = false;
  log.info("Backend connected (session={})", sessionId || "(new)");
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
let _settingsPanelClose: (() => void) | null = null; // 面板关闭句柄，用于 onConfigUpdated 中清理 DOM

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
  if (!actions) { _settingsPanelVisible = false; return; }
  log.info("Config update result: {}", result.success ? "success" : "failed");
  // 后端仅在运行时重建成功且 .env 持久化成功后才发送 success，
  // 所以成功时应立即退出设置页，不再人为停留数秒。
  if (result.success) {
    _settingsPanelClose?.();
    _settingsPanelClose = null;
    _settingsPanelVisible = false;
    return;
  }

  // 清除旧提示
  actions.querySelector(".settings-saved-hint")?.remove();
  const hint = document.createElement("span");
  hint.className = "settings-saved-hint";
  hint.style.fontSize = "12px";
  hint.style.lineHeight = "30px";
  hint.setAttribute("role", "status");
  hint.setAttribute("aria-live", "polite");
  if (result.errors) {
    hint.style.color = "var(--color-status-offline)";
    hint.textContent = result.errors?.join("; ") ?? "配置更新失败";
  }
  actions.insertBefore(hint, actions.firstChild);
};

ws.onStatusChange = (status) => {
  appState.wsStatus = status;
  chat.updateConnectionStatus(status);
  if (status === "disconnected") {
    log.warn("WS disconnected — resetting UI state");
    // 断线时恢复 UI 状态，防止 isReplying 死锁
    // 同时清除会话加载保护状态，防止跨连接残留
    if (_loadTimer !== null) { clearTimeout(_loadTimer); _loadTimer = null; }
    _loadingSession = false;
    _loadingSessionId = null;
    _settingsPanelVisible = false; // 断线时重置设置面板可见性
		_settingsPanelClose?.(); _settingsPanelClose = null;
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
  chat.showAILoading(); // 立即显示 AI ··· loading 气泡
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
  log.info("New chat requested");
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
  log.info("Loading session {}", sessionId);
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
    log.warn("Session load timeout");
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
sidebar.onRenameSession = (sessionId: string, title: string) => {
  ws.renameSession(sessionId, title);
};
sidebar.onClearAll = () => {
  log.info("Clear all sessions requested ({} sessions)", appState.sessions.length);
  const sessions = [...appState.sessions];
  for (const s of sessions) {
    ws.deleteSession(s.id);
  }
};

sidebar.onOpenSettings = async () => {
  if (_settingsPanelVisible) return;
  _settingsPanelVisible = true;
  log.info("Settings panel opened");

  // 请求最新配置并等待返回，用新鲜数据初始化面板（消除 updateData 异步重渲染竞态）
  const freshConfig = await new Promise<SettingsData | null>((resolve) => {
    const orig = ws.onCurrentConfig;
    const timeout = setTimeout(() => { ws.onCurrentConfig = orig; resolve(null); }, 3000);
    ws.onCurrentConfig = (data) => {
      clearTimeout(timeout);
      ws.onCurrentConfig = orig;
      _settingsData = {
        main: data.main,
        memory: data.memory,
        memory_enabled: data.memory_enabled,
      };
      resolve(_settingsData);
    };
    ws.getConfig();
  });

  const initial: SettingsData = freshConfig || {
    main: { api_key: "", base_url: "", model: "" },
    memory: { api_key: "", base_url: "", model: "" },
    memory_enabled: false,
  };
  const triggerBtn = document.querySelector<HTMLElement>(".sidebar__settings-btn") ?? undefined;
  const panel = new SettingsPanel(initial, triggerBtn);
  let closedAfterSave = false;
  _settingsPanelClose = () => {
    closedAfterSave = true;
    panel.close();
  };
  panel.onSave = (result) => {
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

  const result = await panel.show();
  if (!result) {
    if (!closedAfterSave) {
      log.info("Settings panel closed (cancelled)");
    }
    _settingsPanelVisible = false;
    _settingsPanelClose = null;
    return;
  }
};

  // ===== 连接 =====
  _connectWs(ws);
}

// 非 Electron 环境直接初始化
if (!api) { initChatModules(); }

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

