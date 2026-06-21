/**
 * InputController — 输入框 + 发送/停止按钮逻辑
 *
 * 职责：
 * - 管理 textarea 和 button 的 DOM 状态
 * - Enter 发送 / Shift+Enter 换行
 * - 锁定/解锁输入
 * - 发送/停止按钮互斥切换
 */

export class InputController {
  private _textarea: HTMLTextAreaElement;
  private _button: HTMLButtonElement;
  private _locked = false;
  private _sending = false; // AI 回复中 → 显示停止按钮

  // 回调
  onSend?: (text: string) => void;
  onStop?: () => void;

  constructor(textarea: HTMLTextAreaElement, button: HTMLButtonElement) {
    this._textarea = textarea;
    this._button = button;

    this._bindEvents();
    this._updateButtonState();
  }

  // ---- 锁 ----

  lock(): void {
    this._locked = true;
    this._updateButtonState();
  }

  unlock(): void {
    this._locked = false;
    this._sending = false;
    this._updateButtonState();
  }

  // ---- 发送/停止互斥 ----

  showStopButton(): void {
    this._sending = true;
    this._updateButtonState();
  }

  hideStopButton(): void {
    this._sending = false;
    this._updateButtonState();
  }

  // ---- 输入框 ----

  getValue(): string {
    return this._textarea.value;
  }

  setValue(text: string): void {
    this._textarea.value = text;
    this._autoResize();
  }

  clear(): void {
    this._textarea.value = "";
    this._autoResize();
    this._updateButtonState();
  }

  focus(): void {
    this._textarea.focus();
  }

  // ---- 私有 ----

  private _bindEvents(): void {
    this._textarea.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        this._handleSubmit();
      }
    });

    this._textarea.addEventListener("input", () => {
      this._autoResize();
      this._updateButtonState();
    });

    this._button.addEventListener("click", () => {
      this._handleSubmit();
    });
  }

  private _handleSubmit(): void {
    if (this._locked) return;

    if (this._sending) {
      // 当前是停止按钮
      this.onStop?.();
      return;
    }

    const text = this._textarea.value.trim();
    if (!text) return;

    this.onSend?.(text);
    this.clear(); // 发送后清空输入框
  }

  private _updateButtonState(): void {
    if (this._sending) {
      this._button.textContent = "停止";
      this._button.className = "chat-send-btn chat-send-btn--stop";
      this._button.disabled = false;
      return;
    }

    if (this._locked) {
      this._button.textContent = "发送";
      this._button.className = "chat-send-btn chat-send-btn--disabled";
      this._button.disabled = true;
      return;
    }

    const hasContent = this._textarea.value.trim().length > 0;
    this._button.textContent = "发送";
    this._button.className = hasContent
      ? "chat-send-btn"
      : "chat-send-btn chat-send-btn--disabled";
    this._button.disabled = !hasContent;
  }

  private _autoResize(): void {
    this._textarea.style.height = "auto";
    const newHeight = Math.min(this._textarea.scrollHeight, 120);
    this._textarea.style.height = `${Math.max(newHeight, 40)}px`;
  }
}
