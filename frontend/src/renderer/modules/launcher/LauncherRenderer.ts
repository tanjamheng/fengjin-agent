/**
 * LauncherRenderer — 加载页渲染进程
 *
 * 监听主进程 IPC 推送的 LauncherState，更新右边加载区 DOM。
 * 加载完成后触发过渡到聊天界面。
 */

interface LauncherState {
  phase: "env_check" | "scanning" | "preprocess" | "system_load" | "done" | "error";
  phaseLabel: string;
  stepText: string;
  progressPercent: number;
  stepPercent: number;       // 当前步骤内百分比 (0-100)，0=无子进度
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
  private _percentText: HTMLElement;
  private _stepText: HTMLElement;
  private _comfortText: HTMLElement;
  private _errorBox: HTMLElement;
  private _errorText: HTMLElement;
  private _btnRetry: HTMLElement;
  private _btnSkip: HTMLElement;
  private _btnLogs: HTMLElement;
  private _lastPhase: string = "";
  private _comfortIndex: number = -1;
  private _comfortPrev: number[] = []; // 前两次的 index，避免重复
  private _doneFired: boolean = false;
  private _onDone: (() => void) | null = null;
  private _animFrame: number | null = null;
  private _targetPct: number = 0;

  // 风堇安抚语 — 点击可切换
  private static readonly COMFORT_MESSAGES = [
    "别急哦，我在准备呢~",
    "马上就能见面了……",
    "昏光庭院的午后，最适合等待了。",
    "嘿，你今天心情怎么样？",
    "来杯茶吧，很快就好了。",
    "我会好好准备的，不会让你失望。",
    "愿这一抹微光，拨开云雾。",
    "嗯……让我想想，先从哪一步开始呢？",
    "稍微等一下下，值得的。",
    "灰宝，你在外面等着就好~",
    "治愈需要一点时间，耐心也是良药哦。",
    "风堇正在赶来……路上采了几朵花。",
    "每一次等待，都是为了更好的重逢。",
    "别担心，我在呢。",

    "好了好了，就快了……你还真是急性子呢。",
    "今天的我，比昨天更想见你。",
    "呼吸——放轻松，一切都会好的。",
    "昏光庭院的花开了，你要看看吗？",
    "不管多久，我都会在这里等你回来。",
  ];

  constructor(container: HTMLElement) {
    this._container = container;
    this._phaseLabel = container.querySelector("#launcher-phase-label")!;
    this._progressBar = container.querySelector("#launcher-progress-bar")!;
    this._progressFill = container.querySelector("#launcher-progress-fill")!;
    this._percentText = container.querySelector("#launcher-percent")!;
    this._stepText = container.querySelector("#launcher-step-text")!;
    this._comfortText = container.querySelector("#launcher-comfort")!;
    this._errorBox = container.querySelector("#launcher-error")!;
    this._errorText = container.querySelector("#launcher-error-text")!;
    this._btnRetry = container.querySelector("#launcher-btn-retry")!;
    this._btnSkip = container.querySelector("#launcher-btn-skip")!;
    this._btnLogs = container.querySelector("#launcher-btn-logs")!;

    // 随机初始安抚语
    this._pickNextComfort();

    this._bindButtons();
  }

  /** 加载完成后的回调（触发过渡动画） */
  set onDone(cb: () => void) {
    this._onDone = cb;
  }

  /** 主进程推送状态更新 */
  update(state: LauncherState): void {
    const pct = Math.min(100, Math.max(0, state.progressPercent));

    // 阶段标签（用 visibility 而非 display，保证过渡动画生效）
    if (state.phaseLabel !== this._lastPhase) {
      this._phaseLabel.style.opacity = "0";
      setTimeout(() => {
        this._phaseLabel.textContent = state.phaseLabel;
        this._phaseLabel.style.visibility = state.phaseLabel ? "visible" : "hidden";
        if (state.phaseLabel) {
          this._phaseLabel.style.opacity = "1";
        }
      }, 150);
      this._lastPhase = state.phaseLabel;
    }

    // 步骤文字 — 有子进度时追加 (XX%)
    this._stepText.textContent = state.stepPercent > 0
      ? `${state.stepText} (${state.stepPercent}%)`
      : state.stepText;

    // 进度条 + 百分比（平滑动画，防止同步批量 done 消息导致瞬时跳变）
    this._animateProgress(pct);
    if (state.phase === "scanning" || state.phase === "preprocess" || state.phase === "system_load") {
      this._progressBar.style.display = "";
    }

    // 安抚文字 — 所有加载阶段都显示
    if (state.phase === "scanning" || state.phase === "preprocess" || state.phase === "system_load") {
      this._comfortText.style.display = "";
      if (this._comfortIndex < 0) this._pickNextComfort();
      this._comfortText.textContent = LauncherRenderer.COMFORT_MESSAGES[this._comfortIndex];
    } else {
      this._comfortText.style.display = "none";
    }

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
      this._percentText.textContent = "100%";
      this._phaseLabel.textContent = "";
      this._stepText.textContent = "";
      this._lastPhase = "";
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

    // 点击安抚语随机切换（避免与前2次重复）
    this._comfortText.addEventListener("click", () => {
      this._pickNextComfort();
      this._comfortText.textContent = LauncherRenderer.COMFORT_MESSAGES[this._comfortIndex];
      // 切换时短暂闪烁
      this._comfortText.style.opacity = "0.5";
      requestAnimationFrame(() => {
        this._comfortText.style.opacity = "";
      });
    });
  }

  /** 平滑动画进度条——防止同步批量 done 消息导致百分比瞬时跳变 */
  private _animateProgress(target: number): void {
    this._targetPct = target;
    if (this._animFrame !== null) return; // 已有动画进行中，只更新目标值
    const step = () => {
      const cur = parseFloat(this._progressFill.style.width) || 0;
      const diff = this._targetPct - cur;
      if (Math.abs(diff) < 0.5) {
        this._progressFill.style.width = `${this._targetPct}%`;
        this._percentText.textContent = `${this._targetPct}%`;
        this._animFrame = null;
        return;
      }
      // ease-out: 每帧移动剩余距离的 85%（快速追赶，避免画面延迟感）
      const next = cur + diff * 0.85;
      this._progressFill.style.width = `${next}%`;
      this._percentText.textContent = `${Math.round(next)}%`;
      this._animFrame = requestAnimationFrame(step);
    };
    this._animFrame = requestAnimationFrame(step);
  }

  /** 随机选取安抚语，排除前2次出现过的（拒绝采样，O(1) 期望） */
  private _pickNextComfort(): void {
    const total = LauncherRenderer.COMFORT_MESSAGES.length;
    let pick: number;
    do {
      pick = Math.floor(Math.random() * total);
    } while (this._comfortPrev.includes(pick) && this._comfortPrev.length < total);
    this._comfortIndex = pick;
    this._comfortPrev.push(pick);
    if (this._comfortPrev.length > 2) this._comfortPrev.shift();
  }

  private _triggerDone(): void {
    if (this._doneFired) return;
    this._doneFired = true;
    // 延迟一帧，让 100% 进度条渲染出来
    requestAnimationFrame(() => {
      if (this._onDone) this._onDone();
    });
  }
}
