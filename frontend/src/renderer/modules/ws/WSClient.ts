import { MessageParser } from "./MessageParser";
import type {
  ClientMessage,
  ConnectionStatus,
  ServerMessage,
  SessionMeta,
  ChatMessage,
} from "../../types/protocol";

/**
 * WSClient — WebSocket 连接管理 + 心跳 + 消息收发
 *
 * 通过回调将解析后的消息分发给上层模块。
 * 上层不应直接操作 WebSocket，全部通过此类的方法。
 */
export class WSClient {
  private _ws: WebSocket | null = null;
  private _url: string = "";
  private _status: ConnectionStatus = "disconnected";
  private _sessionId: string = "";
  private _parser = new MessageParser();

  // 心跳
  private _pingTimer: ReturnType<typeof setInterval> | null = null;
  private _pongTimer: ReturnType<typeof setTimeout> | null = null;
  private readonly _pingInterval = 30000; // 30s
  private readonly _pongTimeout = 10000; // 10s

  // 回复超时
  private _replyTimer: ReturnType<typeof setTimeout> | null = null;
  private readonly _replyTimeout = 60000; // 60s

  // ---- 回调（由上层注册） ----

  onStatusChange?: (status: ConnectionStatus) => void;
  onStreamChunk?: (text: string) => void;
  onStreamEnd?: (fullText: string, action?: string) => void;
  onBlocked?: (message: string, category?: string) => void;
  onThinking?: () => void;
  onError?: (message: string) => void;

  // 会话回调
  onSessionList?: (sessions: SessionMeta[]) => void;
  onSessionLoaded?: (sessionId: string, title: string, messages: ChatMessage[]) => void;
  onSessionDeleted?: (sessionId: string) => void;
  onQuickReplies?: (replies: string[]) => void;

  // 连接确认回调
  onConnected?: (sessionId: string) => void;

  // ---- 公开属性 ----

  get status(): ConnectionStatus {
    return this._status;
  }

  get sessionId(): string {
    return this._sessionId;
  }

  // ===== 连接管理 =====

  connect(url: string): void {
    if (this._ws) {
      this.disconnect();
    }

    this._url = url;
    this._setStatus("connecting");
    this._parser.resetErrors();

    try {
      this._ws = new WebSocket(url);
    } catch {
      this._setStatus("disconnected");
      this.onError?.("无法创建 WebSocket 连接");
      return;
    }

    this._ws.onopen = this._onOpen.bind(this);
    this._ws.onmessage = this._onMessage.bind(this);
    this._ws.onclose = this._onClose.bind(this);
    this._ws.onerror = this._onError.bind(this);
  }

  disconnect(): void {
    this._stopHeartbeat();
    this._clearReplyTimer();
    if (this._ws) {
      this._ws.onopen = null;
      this._ws.onmessage = null;
      this._ws.onclose = null;
      this._ws.onerror = null;
      if (
        this._ws.readyState === WebSocket.OPEN ||
        this._ws.readyState === WebSocket.CONNECTING
      ) {
        this._ws.close(1000);
      }
      this._ws = null;
    }
    this._connectedBefore = false;
    this._setStatus("disconnected");
  }

  // ===== 公开方法 =====

  /** 重置会话 ID（新对话/删除当前会话时调用） */
  resetSessionId(): void {
    this._sessionId = "";
  }

  // ===== 发送 =====

  sendUserMessage(content: string): void {
    if (this._send({ type: "user_msg", session_id: this._sessionId, content })) {
      this._startReplyTimer();
    }
  }

  sendCancel(): void {
    this._send({ type: "cancel" });
    this._clearReplyTimer();
  }

  listSessions(): void {
    this._send({ type: "list_sessions" });
  }

  loadSession(sessionId: string): void {
    this._send({ type: "load_session", session_id: sessionId });
  }

  deleteSession(sessionId: string): void {
    this._send({ type: "delete_session", session_id: sessionId });
  }

  // ===== 内部方法 =====

  /** @returns true 如果消息已发送 */
  private _send(msg: ClientMessage): boolean {
    if (this._ws?.readyState !== WebSocket.OPEN) {
      // 连接已断开时立即反馈，不让用户等 60s 超时
      this._clearReplyTimer();
      this.onError?.("WebSocket 连接已断开，请重连");
      return false;
    }
    this._ws.send(JSON.stringify(msg));
    return true;
  }

  private _setStatus(status: ConnectionStatus): void {
    if (this._status !== status) {
      this._status = status;
      this.onStatusChange?.(status);
    }
  }

  // ---- 事件处理 ----

  private _onOpen(): void {
    // connected 消息由后端主动发送，不在此处设置状态
    this._startHeartbeat();
  }

  private _onMessage(event: MessageEvent): void {
    const msg = this._parser.parse(event.data as string);
    if (!msg) {
      if (this._parser.errorState) {
        this.onError?.("AI 回复解析异常");
        this._parser.resetErrors();
      }
      return;
    }

    this._dispatch(msg);
  }

  private _connectedBefore = false;

  private _onClose(_event: CloseEvent): void {
    this._stopHeartbeat();
    this._clearReplyTimer();
    const wasConnected = this._connectedBefore;
    this._connectedBefore = false;
    this._setStatus("disconnected");
    if (wasConnected) {
      this.onError?.("WebSocket 连接已断开，请检查后端服务");
    }
  }

  private _onError(_event: Event): void {
    // onerror 之后通常会触发 onclose，由 onclose 统一处理
  }

  // ---- 消息分发 ----

  private _dispatch(msg: ServerMessage): void {
    switch (msg.type) {
      case "connected":
        this._sessionId = msg.session_id;
        this._connectedBefore = true;
        this._setStatus("connected");
        this.onConnected?.(msg.session_id);
        break;

      case "pong":
        this._onPongReceived();
        break;

      case "thinking":
        // 服务端在响应，刷新回复超时计时器而非清除
        this._startReplyTimer();
        this.onThinking?.();
        break;

      case "blocked":
        this._clearReplyTimer();
        this.onBlocked?.(msg.message ?? "小伊卡提醒：暂无法处理该消息", msg.category);
        break;

      case "stream":
        // 服务端在流式输出，刷新超时计时器
        this._startReplyTimer();
        this.onStreamChunk?.(msg.text ?? "");
        break;

      case "end":
        this._clearReplyTimer();
        this.onStreamEnd?.(msg.full_text ?? "", msg.action);
        break;

      case "session_list":
        this.onSessionList?.(msg.sessions ?? []);
        break;

      case "session_loaded":
        this._sessionId = msg.session_id;
        this.onSessionLoaded?.(msg.session_id, msg.title, msg.messages ?? []);
        break;

      case "session_deleted":
        this.onSessionDeleted?.(msg.session_id);
        break;

      case "quick_replies":
        this.onQuickReplies?.(msg.replies ?? []);
        break;

      case "error":
        this._clearReplyTimer();
        this.onError?.(msg.message ?? "AI 服务异常，请稍后重试");
        break;

      default:
        // 未知 type：忽略，不崩溃
        break;
    }
  }

  // ---- 心跳 ----

  private _startHeartbeat(): void {
    this._stopHeartbeat();
    this._pingTimer = setInterval(() => {
      this._send({ type: "ping" });
      // 先清除上次的 pong 超时定时器，防止 pongTimeout > pingInterval 时残留
      if (this._pongTimer !== null) {
        clearTimeout(this._pongTimer);
        this._pongTimer = null;
      }
      this._pongTimer = setTimeout(() => {
        // 10s 未收到 pong，判定断线
        this.disconnect();
      }, this._pongTimeout);
    }, this._pingInterval);
  }

  private _stopHeartbeat(): void {
    if (this._pingTimer !== null) {
      clearInterval(this._pingTimer);
      this._pingTimer = null;
    }
    if (this._pongTimer !== null) {
      clearTimeout(this._pongTimer);
      this._pongTimer = null;
    }
  }

  private _onPongReceived(): void {
    if (this._pongTimer !== null) {
      clearTimeout(this._pongTimer);
      this._pongTimer = null;
    }
  }

  // ---- 回复超时 ----

  private _startReplyTimer(): void {
    this._clearReplyTimer();
    this._replyTimer = setTimeout(() => {
      this.sendCancel(); // 通知后端停止处理，避免残留 stream/end 幽灵消息
      this.onError?.("回复超时，请重试");
      this._replyTimer = null;
    }, this._replyTimeout);
  }

  private _clearReplyTimer(): void {
    if (this._replyTimer !== null) {
      clearTimeout(this._replyTimer);
      this._replyTimer = null;
    }
  }
}
