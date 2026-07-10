/**
 * Electron API 类型声明
 * 对应 preload.ts 中通过 contextBridge 暴露的 API
 */
interface ElectronAPI {
  minimize: () => void;
  maximize: () => void;
  close: () => void;
  toggleAlwaysOnTop: () => void;
  onLauncherState: (callback: (state: unknown) => void) => void;
  onLauncherMode: (callback: (mode: string) => void) => void;
  onLauncherNeedConfig: (callback: () => void) => void;
  launcherRetry: () => Promise<void>;
  launcherSkip: () => Promise<void>;
  launcherGetState: () => Promise<unknown>;
  getWsUrl: () => Promise<string>;
  settingsWriteEnv: (data: unknown) => Promise<{ success: boolean; error?: string }>;
  openLogs: () => Promise<void>;
  openUrl: (url: string) => Promise<{ success: boolean; error?: string }>;
}

interface Window {
  electronAPI?: ElectronAPI;
}
