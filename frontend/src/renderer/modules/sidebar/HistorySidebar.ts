import type { SessionMeta } from "../../types/protocol";
import { showConfirm } from "../../utils/dialog";

/**
 * HistorySidebar — 历史侧边栏 DOM 管理
 *
 * 通过 WSClient 调用后端 SessionManager（不自行管理会话存储）。
 * 渲染会话列表，支持切换/删除/新建/清空全部。
 */
export class HistorySidebar {
  private _container: HTMLElement;
  private _listEl: HTMLElement;
  private _emptyEl: HTMLElement;
  private _activeId: string = "";
  private _sessions: SessionMeta[] = [];
  private _disabled = false;

  // 回调
  onNewChat?: () => void;
  onSelectSession?: (sessionId: string) => void;
  onDeleteSession?: (sessionId: string) => void;
  onClearAll?: () => void;
  onOpenSettings?: () => void;

  constructor(container: HTMLElement) {
    this._container = container;
    this._container.classList.add("sidebar");

    // 头部
    const header = document.createElement("div");
    header.className = "sidebar__header";

    const newChatBtn = document.createElement("button");
    newChatBtn.className = "sidebar__new-chat-btn";
    newChatBtn.textContent = "+ 新对话";
    newChatBtn.addEventListener("click", () => {
      if (!this._disabled) this.onNewChat?.();
    });
    header.appendChild(newChatBtn);

    const settingsBtn = document.createElement("button");
    settingsBtn.className = "sidebar__settings-btn";
    settingsBtn.textContent = "⚙ 设置";
    settingsBtn.setAttribute("aria-label", "设置");
    settingsBtn.title = "设置";
    settingsBtn.addEventListener("click", () => {
      if (!this._disabled) this.onOpenSettings?.();
    });
    header.appendChild(settingsBtn);

    this._container.appendChild(header);

    // 列表
    this._listEl = document.createElement("div");
    this._listEl.className = "sidebar__list";
    this._container.appendChild(this._listEl);

    // 空状态
    this._emptyEl = document.createElement("div");
    this._emptyEl.className = "sidebar__empty";
    this._emptyEl.textContent = "暂无历史对话";
    this._emptyEl.style.display = "none";
    this._listEl.appendChild(this._emptyEl);

    // 底部（清空全部按钮暂时隐藏，后续移至设置面板）
    // const footer = document.createElement("div");
    // footer.className = "sidebar__footer";
    // const clearAllBtn = ...
  }

  /** 渲染会话列表 */
  renderList(sessions: SessionMeta[]): void {
    this._sessions = sessions;
    this._listEl.querySelectorAll(".sidebar__item").forEach((el) => el.remove());

    if (sessions.length === 0) {
      this._emptyEl.style.display = "block";
      return;
    }

    this._emptyEl.style.display = "none";

    for (const session of sessions) {
      const item = this._createSessionItem(session);
      this._listEl.insertBefore(item, this._emptyEl);
    }
  }

  /** 高亮当前活跃会话 */
  setActive(sessionId: string): void {
    this._activeId = sessionId;
    this._listEl.querySelectorAll(".sidebar__item").forEach((el) => {
      const id = (el as HTMLElement).dataset.sessionId;
      el.classList.toggle("sidebar__item--active", id === sessionId);
    });
  }

  /** 空状态 */
  showEmpty(): void {
    this._sessions = [];
    this._listEl.querySelectorAll(".sidebar__item").forEach((el) => el.remove());
    this._emptyEl.style.display = "block";
  }

  /** 禁用全部交互（流式回复中） */
  setDisabled(disabled: boolean): void {
    this._disabled = disabled;
    this._listEl.querySelectorAll(".sidebar__item").forEach((el) => {
      (el as HTMLElement).style.pointerEvents = disabled ? "none" : "";
      (el as HTMLElement).style.opacity = disabled ? "0.5" : "";
    });
  }

  // ---- 私有 ----

  private _createSessionItem(session: SessionMeta): HTMLElement {
    const item = document.createElement("div");
    item.className = "sidebar__item";
    item.dataset.sessionId = session.id;
    if (session.id === this._activeId) {
      item.classList.add("sidebar__item--active");
    }

    // 键盘/点击切换
    item.setAttribute("tabindex", "0");
    item.setAttribute("role", "button");
    item.addEventListener("click", () => {
      if (!this._disabled) this.onSelectSession?.(session.id);
    });
    item.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        if (!this._disabled) this.onSelectSession?.(session.id);
      }
    });

    // 标题
    const title = document.createElement("div");
    title.className = "sidebar__item-title";
    title.textContent = session.title || "新对话";
    item.appendChild(title);

    // 副标题
    const meta = document.createElement("div");
    meta.className = "sidebar__item-meta";
    meta.textContent = `${session.message_count} 条消息 · ${this._formatRelativeTime(
      session.updated_at
    )}`;
    item.appendChild(meta);

    // 删除按钮（hover 显示）
    const delBtn = document.createElement("button");
    delBtn.className = "sidebar__item-delete";
    delBtn.textContent = "×";
    delBtn.setAttribute("aria-label", `删除会话: ${session.title}`);
    delBtn.title = "删除此会话";
    delBtn.addEventListener("click", async (e) => {
      e.stopPropagation();
      if (this._disabled) return;
      const ok = await showConfirm(`确定删除会话「${session.title || "新对话"}」？此操作不可撤销`);
      if (!ok) return;
      this.onDeleteSession?.(session.id);
    });
    item.appendChild(delBtn);

    return item;
  }

  private _formatRelativeTime(isoStr: string): string {
    const now = Date.now();
    const ts = new Date(isoStr).getTime();
    const diffMs = now - ts;

    if (diffMs < 60_000) return "刚刚";
    const mins = Math.floor(diffMs / 60_000);
    if (mins < 60) return `${mins} 分钟前`;
    const hours = Math.floor(mins / 60);
    if (hours < 24) return `${hours} 小时前`;
    const days = Math.floor(hours / 24);
    if (days < 7) return `${days} 天前`;
    return "更早";
  }
}
