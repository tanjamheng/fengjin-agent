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

export interface ClientGetConfig {
  type: "get_config";
}

export interface ClientUpdateConfig {
  type: "update_config";
  main: { api_key: string | null; base_url: string | null; model: string | null };
  memory: { api_key: string | null; base_url: string | null; model: string | null };
  memory_enabled: boolean;
}

export type ClientMessage =
  | ClientUserMsg
  | ClientPing
  | ClientCancel
  | ClientListSessions
  | ClientLoadSession
  | ClientDeleteSession
  | ClientGetConfig
  | ClientUpdateConfig;

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
  session_id?: string;
}

export interface ServerBlocked {
  type: "blocked";
  message: string;
  category?: string;
  session_id?: string;
}

export interface ServerStream {
  type: "stream";
  text: string;
}

export interface ServerEnd {
  type: "end";
  full_text: string;
  action?: string;
  session_id?: string;
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
  session_id?: string;
}

export interface ServerCurrentConfig {
  type: "current_config";
  main: { api_key: string; base_url: string; model: string };
  memory: { api_key: string; base_url: string; model: string };
  memory_enabled: boolean;
}

export interface ServerConfigUpdated {
  type: "config_updated";
  success: boolean;
  errors?: string[];
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
  | ServerError
  | ServerCurrentConfig
  | ServerConfigUpdated;
