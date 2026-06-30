/**
 * InputController — 输入框 + 发送/停止按钮逻辑
 *
 * 职责：
 * - 管理 textarea 和 button 的 DOM 状态
 * - Enter 发送 / Shift+Enter 换行
 * - 锁定/解锁输入
 * - 发送/停止按钮互斥切换
 */

import { CONFIG } from "../../config";

/** 发送按钮图标 — 圆形向上箭头 */
const SEND_ICON =
  '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="8" y1="2" x2="8" y2="13"/><polyline points="4 6 8 2 12 6"/></svg>';

/** 停止按钮图标 — 实心方块 */
const STOP_ICON =
  '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><rect x="3.5" y="3.5" width="9" height="9" rx="2"/></svg>';

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

  private _submitting = false;

  private _handleSubmit(): void {
    // _sending 必须先于 _locked 检查——AI 回复中 _locked=true 且 _sending=true，
    // 若先检查 _locked 则停止按钮永远不可达（P0 死锁）
    if (this._sending) {
      this.onStop?.();
      return;
    }

    if (this._locked) return;

    // 防抖：防止停止按钮后立即触发发送（双击停止→发送）
    if (this._submitting) return;
    this._submitting = true;
    setTimeout(() => { this._submitting = false; }, CONFIG.input.submitDebounceMs);

    const text = this._textarea.value.trim();
    if (!text) { this._submitting = false; return; }

    this.onSend?.(text);
    this.clear(); // 发送后清空输入框
  }

  private _updateButtonState(): void {
    // 图标按钮统一设置 aria-label
    if (this._sending) {
      this._button.innerHTML = STOP_ICON;
      this._button.setAttribute("aria-label", "停止");
      this._button.className = "chat-send-btn chat-send-btn--stop";
      this._button.disabled = false;
      return;
    }

    if (this._locked) {
      this._button.innerHTML = SEND_ICON;
      this._button.setAttribute("aria-label", "发送");
      this._button.className = "chat-send-btn chat-send-btn--disabled";
      this._button.disabled = true;
      return;
    }

    const hasContent = this._textarea.value.trim().length > 0;
    this._button.innerHTML = SEND_ICON;
    this._button.setAttribute("aria-label", "发送");
    this._button.className = hasContent
      ? "chat-send-btn"
      : "chat-send-btn chat-send-btn--disabled";
    this._button.disabled = !hasContent;
  }

  private _autoResize(): void {
    this._textarea.style.height = "auto";
    // border-box 下 height 含 border，而 scrollHeight 只含 padding+content。
    // 必须补上 border 厚度，否则内容刚好填满时会少算 border → 误显滚动条 + 滚动条穿出。
    const cs = getComputedStyle(this._textarea);
    const borderH = parseFloat(cs.borderTopWidth) + parseFloat(cs.borderBottomWidth);
    const target = Math.min(this._textarea.scrollHeight + borderH, CONFIG.input.maxHeight);
    this._textarea.style.height = `${Math.max(target, CONFIG.input.minHeight)}px`;
  }
}
