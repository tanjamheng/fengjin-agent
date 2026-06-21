import type { ConnectionStatus, SessionMeta } from "./types/protocol";

/**
 * AppState — 中心状态管理（V1：简单对象 + 回调，不引入状态管理库）
 */

export interface AppState {
  wsStatus: ConnectionStatus;
  isReplying: boolean;
  isModelLoaded: boolean;
  isScrolledToBottom: boolean;
  currentSessionId: string;
  sessions: SessionMeta[];
}

export const appState: AppState = {
  wsStatus: "disconnected",
  isReplying: false,
  isModelLoaded: false,
  isScrolledToBottom: true,
  currentSessionId: "",
  sessions: [],
};
