// ===== WS 协议类型定义 =====
// 权威规格源：核心文档/核心4_WS通信协议.md
// 本文档是派生实现，协议变更时与核心4同步更新

// ---- 基础类型 ----

export type ConnectionStatus = "connecting" | "connected" | "disconnected";

export interface SessionMeta {
  id: string;
  title: string;
  message_count: number;
  created_at: string; // ISO 8601
  updated_at: string; // ISO 8601
}

export interface ChatMessage {
  role: "user" | "assistant" | "system";
  content: string;
  timestamp: string; // ISO 8601
}

// ---- Client → Server 消息 ----

export interface ClientUserMsg {
  type: "user_msg";
  session_id: string;
  content: string;
}

export interface ClientPing {
  type: "ping";
}

export interface ClientCancel {
  type: "cancel";
}

export interface ClientListSessions {
  type: "list_sessions";
}

export interface ClientLoadSession {
  type: "load_session";
  session_id: string;
}

export interface ClientDeleteSession {
  type: "delete_session";
  session_id: string;
}

export type ClientMessage =
  | ClientUserMsg
  | ClientPing
  | ClientCancel
  | ClientListSessions
  | ClientLoadSession
  | ClientDeleteSession;

// ---- Server → Client 消息 ----

export interface ServerConnected {
  type: "connected";
  session_id: string;
}

export interface ServerPong {
  type: "pong";
}

export interface ServerThinking {
  type: "thinking";
}

export interface ServerBlocked {
  type: "blocked";
  message: string;
  category?: string;
}

export interface ServerStream {
  type: "stream";
  text: string;
}

export interface ServerEnd {
  type: "end";
  full_text: string;
  action?: string;
}

export interface ServerSessionList {
  type: "session_list";
  sessions: SessionMeta[];
}

export interface ServerSessionLoaded {
  type: "session_loaded";
  session_id: string;
  title: string;
  messages: ChatMessage[];
}

export interface ServerSessionDeleted {
  type: "session_deleted";
  session_id: string;
}

export interface ServerQuickReplies {
  type: "quick_replies";
  replies: string[];
}

export interface ServerError {
  type: "error";
  message: string;
}

export type ServerMessage =
  | ServerConnected
  | ServerPong
  | ServerThinking
  | ServerBlocked
  | ServerStream
  | ServerEnd
  | ServerSessionList
  | ServerSessionLoaded
  | ServerSessionDeleted
  | ServerQuickReplies
  | ServerError;
