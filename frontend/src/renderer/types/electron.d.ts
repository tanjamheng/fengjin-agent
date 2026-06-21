/**
 * Electron API 类型声明
 * 对应 preload.ts 中通过 contextBridge 暴露的 API
 */
interface ElectronAPI {
  minimize: () => void;
  maximize: () => void;
  close: () => void;
  toggleAlwaysOnTop: () => void;
}

interface Window {
  electronAPI?: ElectronAPI;
}
