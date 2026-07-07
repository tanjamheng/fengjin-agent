/**
 * LauncherRenderer — 加载页渲染进程
 *
 * 监听主进程 IPC 推送的 LauncherState，更新右边加载区 DOM。
 * 加载完成后触发过渡到聊天界面。
 */

interface LauncherState {
  phase: "env_check" | "preprocess" | "system_load" | "done" | "error";
  phaseLabel: string;
  stepText: string;
  progressPercent: number;
  showComfort: boolean;
  preprocessSteps: string[];
  currentStepIndex: number;
  error: string | null;
  showRetry: boolean;
  showSkip: boolean;
  showLogs: boolean;
}

export class LauncherRenderer {
  private _container: HTMLElement;
  private _phaseLabel: HTMLElement;
  private _progressBar: HTMLElement;
  private _progressFill: HTMLElement;
  private _stepText: HTMLElement;
  private _comfortText: HTMLElement;
  private _errorBox: HTMLElement;
  private _errorText: HTMLElement;
  private _btnRetry: HTMLElement;
  private _btnSkip: HTMLElement;
  private _btnLogs: HTMLElement;
  private _onDone: (() => void) | null = null;

  constructor(container: HTMLElement) {
    this._container = container;
    this._phaseLabel = container.querySelector("#launcher-phase-label")!;
    this._progressBar = container.querySelector("#launcher-progress-bar")!;
    this._progressFill = container.querySelector("#launcher-progress-fill")!;
    this._stepText = container.querySelector("#launcher-step-text")!;
    this._comfortText = container.querySelector("#launcher-comfort")!;
    this._errorBox = container.querySelector("#launcher-error")!;
    this._errorText = container.querySelector("#launcher-error-text")!;
    this._btnRetry = container.querySelector("#launcher-btn-retry")!;
    this._btnSkip = container.querySelector("#launcher-btn-skip")!;
    this._btnLogs = container.querySelector("#launcher-btn-logs")!;

    this._bindButtons();
  }

  /** 加载完成后的回调（触发过渡动画） */
  set onDone(cb: () => void) {
    this._onDone = cb;
  }

  /** 主进程推送状态更新 */
  update(state: LauncherState): void {
    // 阶段标签
    this._phaseLabel.textContent = state.phaseLabel;
    this._phaseLabel.style.display = state.phaseLabel ? "" : "none";

    // 步骤文字
    this._stepText.textContent = state.stepText;

    // 进度条
    const pct = Math.min(100, Math.max(0, state.progressPercent));
    this._progressFill.style.width = `${pct}%`;
    if (state.phase === "preprocess" || state.phase === "system_load") {
      this._progressBar.style.display = "";
    }

    // 安抚文字
    this._comfortText.style.display = state.showComfort ? "" : "none";

    // 错误
    if (state.error) {
      this._errorText.textContent = state.error;
      this._errorBox.style.display = "";
    } else {
      this._errorBox.style.display = "none";
    }

    // 按钮
    this._btnRetry.style.display = state.showRetry ? "" : "none";
    this._btnSkip.style.display = state.showSkip ? "" : "none";
    this._btnLogs.style.display = state.showLogs ? "" : "none";

    // 完成
    if (state.phase === "done") {
      this._progressFill.style.width = "100%";
      this._phaseLabel.textContent = "";
      this._stepText.textContent = "";
      this._triggerDone();
    }
  }

  private _bindButtons(): void {
    this._btnRetry.addEventListener("click", () => {
      (window as any).electronAPI?.launcherRetry();
    });
    this._btnSkip.addEventListener("click", () => {
      (window as any).electronAPI?.launcherSkip();
    });
    this._btnLogs.addEventListener("click", () => {
      (window as any).electronAPI?.openLogs();
    });
  }

  private _triggerDone(): void {
    // 延迟一帧，让 100% 进度条渲染出来
    requestAnimationFrame(() => {
      if (this._onDone) this._onDone();
    });
  }
}
