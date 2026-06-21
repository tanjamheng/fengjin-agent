import type { ChatMessage } from "../../types/protocol";

/**
 * MessageRenderer — 消息气泡 DOM 渲染
 *
 * 负责创建用户/AI/系统消息的 DOM 元素。
 * 由 ChatUI 调用，不直接操作聊天区容器。
 */

export class MessageRenderer {
  private _container: HTMLElement;
  private _aiBubble: HTMLElement | null = null; // 当前流式 AI 气泡
  private _lastMessageTime: number = 0; // 上一条消息时间戳

  constructor(container: HTMLElement) {
    this._container = container;
  }

  /** 追加用户消息气泡 */
  appendUserMessage(content: string): void {
    this._finalizeAIBubble(); // 结束流式气泡
    this._maybeAppendTimestamp();

    const wrapper = this._createWrapper("user");
    const bubble = this._createBubble("user", content);
    wrapper.appendChild(bubble);
    this._container.appendChild(wrapper);
    this._updateLastTime();
  }

  /** 流式追加 AI 文字分片（无则创建新气泡） */
  appendAIStreamChunk(text: string): void {
    if (!this._aiBubble) {
      const wrapper = this._createWrapper("ai");

      // 星星图标
      const star = document.createElement("span");
      star.className = "chat-message__star";
      star.textContent = "★"; // ★
      star.setAttribute("aria-label", "风堇回复");
      wrapper.appendChild(star);

      this._aiBubble = document.createElement("div");
      this._aiBubble.className = "chat-message--ai";
      wrapper.appendChild(this._aiBubble);
      this._container.appendChild(wrapper);
      this._updateLastTime();
    }
    this._aiBubble.textContent += text;
  }

  /** 固化流式 AI 消息 */
  finalizeAIMessage(fullText: string): void {
    if (this._aiBubble) {
      // fullText 为空时回退到已拼接的流式内容
      const text = fullText || this._aiBubble.textContent || "";
      if (!text) {
        // 没有任何文本内容，移除幽灵气泡
        const wrapper = this._aiBubble.parentElement;
        if (wrapper) wrapper.remove();
        this._aiBubble = null;
        return;
      }
      this._aiBubble.textContent = text;
      this._aiBubble = null;
    }
  }

  /** 追加系统提示消息 */
  appendSystemMessage(
    text: string,
    type: "info" | "warning" | "blocked" = "info"
  ): void {
    this._finalizeAIBubble();

    const el = document.createElement("div");
    el.className = "chat-message--system";
    if (type === "blocked") {
      el.classList.add("chat-message--system--blocked");
      el.setAttribute("role", "alert");
    } else if (type === "warning") {
      el.classList.add("chat-message--system--warning");
      el.setAttribute("role", "alert");
    }
    el.textContent = text;
    this._container.appendChild(el);
  }

  /** 加载历史消息（批量渲染） */
  loadMessages(messages: ChatMessage[]): void {
    this.clear();
    if (!Array.isArray(messages)) return;
    for (const msg of messages) {
      const wrapper = this._createWrapper(msg.role);
      if (msg.role === "assistant") {
        const star = document.createElement("span");
        star.className = "chat-message__star";
        star.textContent = "★";
        star.setAttribute("aria-label", "风堇回复");
        wrapper.appendChild(star);
      }
      const bubble = this._createBubble(msg.role, msg.content);
      wrapper.appendChild(bubble);
      this._container.appendChild(wrapper);
    }
    if (messages.length > 0) {
      const last = messages[messages.length - 1];
      this._lastMessageTime = new Date(last.timestamp).getTime();
    }
  }

  /** 清空所有消息 */
  clear(): void {
    this._aiBubble = null;
    this._lastMessageTime = 0;
    this._container.innerHTML = "";
  }

  /** 当前是否有未固化的流式气泡 */
  get isStreaming(): boolean {
    return this._aiBubble !== null;
  }

  // ---- 私有 ----

  private _finalizeAIBubble(): void {
    // 若流式气泡未收到 end 包但新消息到来，移除 DOM 元素避免"幽灵气泡"
    if (this._aiBubble) {
      const wrapper = this._aiBubble.parentElement;
      if (wrapper) wrapper.remove();
      this._aiBubble = null;
    }
  }

  private _createWrapper(role: string): HTMLElement {
    const wrapper = document.createElement("div");
    wrapper.className = `chat-message-wrapper chat-message-wrapper--${role}`;
    return wrapper;
  }

  private _createBubble(role: string, content: string): HTMLElement {
    const bubble = document.createElement("div");
    bubble.className = `chat-message--${role}`;
    bubble.textContent = content;
    return bubble;
  }

  private _maybeAppendTimestamp(): void {
    const now = Date.now();
    if (this._lastMessageTime > 0 && now - this._lastMessageTime > 5 * 60 * 1000) {
      const timeEl = document.createElement("div");
      timeEl.className = "chat-message__time";
      timeEl.textContent = this._formatTime(now);
      this._container.appendChild(timeEl);
    }
  }

  private _updateLastTime(): void {
    this._lastMessageTime = Date.now();
  }

  private _formatTime(ts: number): string {
    const d = new Date(ts);
    const now = new Date();
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    if (
      d.getFullYear() === now.getFullYear() &&
      d.getMonth() === now.getMonth() &&
      d.getDate() === now.getDate()
    ) {
      return `${hh}:${mm}`;
    }
    const month = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return `${month}-${day} ${hh}:${mm}`;
  }
}
