/**
 * SettingsPanel — 设置面板（左侧 Tab 导航 + 右侧内容区）
 *
 * 复用 dialog.ts 的弹窗画风。当前只有一个 tab："模型配置"。
 * 未来新增 tab 只需在 TABS 数组中追加条目。
 */

type TabId = "model";

interface TabDef {
  id: TabId;
  label: string;
  icon: string;
}

const TABS: TabDef[] = [
  { id: "model", label: "模型配置", icon: "⚙" },
];

/** 眼睛图标 — 闭眼（密码隐藏中，点击显示） */
const EYE_HIDDEN =
  '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94"/><path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19"/><path d="M14.12 14.12a3 3 0 1 0-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>';
/** 眼睛图标 — 睁眼（密码可见中，点击隐藏） */
const EYE_VISIBLE =
  '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>';

export interface ModelConfig {
  api_key: string | null;  // null = 不改
  base_url: string | null;
  model: string | null;
}

export interface SettingsData {
  main: ModelConfig;
  mind: ModelConfig;
  mind_enabled: boolean;
}

export class SettingsPanel {
  private _overlay!: HTMLElement;
  private _activeTab: TabId = "model";
  private _data: SettingsData;
  private _resolve: ((result: SettingsData | null) => void) | null = null;
  private _triggerEl: HTMLElement | null = null;

  // 脏跟踪
  private _dirty = false;

  // 原始 API Key（用于判断用户是否修改了密钥字段）
  private _originalKeys: { main: string; mind: string } = { main: "", mind: "" };

  // Tab 按钮引用
  private _tabBtns = new Map<TabId, HTMLElement>();

  // 可聚焦元素缓存（用于焦点陷阱）
  private _focusableElements: HTMLElement[] = [];
  private _focusedIndex = -1;

  private _saveLabel: string;
  private _hintText: string | null;
  onSave?: (data: SettingsData) => void;

  constructor(initialData: SettingsData, triggerEl?: HTMLElement, saveLabel?: string, hintText?: string) {
    this._data = JSON.parse(JSON.stringify(initialData));
    this._originalKeys = {
      main: this._data.main.api_key ?? "",
      mind: this._data.mind.api_key ?? "",
    };
    this._triggerEl = triggerEl || null;
    this._saveLabel = saveLabel || "保存并应用";
    this._hintText = hintText || null;
  }

  /** 显示面板。返回 null 表示取消，返回 SettingsData 表示确认 */
  show(): Promise<SettingsData | null> {
    return new Promise((resolve) => {
      this._resolve = resolve;
      this._dirty = false;
      this._build();
      document.body.appendChild(this._overlay);
      requestAnimationFrame(() => {
        this._overlay.classList.add("dialog-overlay--visible");
      });
    });
  }

  /** 外部关闭面板（不触发 resolve，仅清理 DOM） */
  close(): void {
    this._close(null, true);
  }

  /** 更新内存中的数据（get_config 返回后调用） */
  updateData(data: SettingsData): void {
    this._data = JSON.parse(JSON.stringify(data));
    this._originalKeys = {
      main: this._data.main.api_key ?? "",
      mind: this._data.mind.api_key ?? "",
    };
    this._dirty = false;
    this._renderModelTab();
  }

  // ---- 构建 ----

  private _build(): void {
    this._overlay = document.createElement("div");
    this._overlay.className = "dialog-overlay settings-overlay";
    this._overlay.setAttribute("role", "dialog");
    this._overlay.setAttribute("aria-modal", "true");
    this._overlay.setAttribute("aria-label", "设置");

    // 点击遮罩关闭
    this._overlay.addEventListener("click", (e) => {
      if (e.target === this._overlay && !this._saving) this._close(null);
    });

    const card = document.createElement("div");
    card.className = "settings-card";
    card.addEventListener("click", (e) => e.stopPropagation());

    // 顶部装饰条
    const topBar = document.createElement("div");
    topBar.className = "dialog-topbar";

    // 标题
    const header = document.createElement("div");
    header.className = "settings-header";
    const h = document.createElement("span");
    h.textContent = "⚙ 设置";
    h.id = "settings-dialog-title";
    this._overlay.setAttribute("aria-labelledby", "settings-dialog-title");
    header.appendChild(h);

    // 首次配置提示横幅
    if (this._hintText) {
      const hintBanner = document.createElement("div");
      hintBanner.className = "settings-hint-banner";
      hintBanner.textContent = this._hintText;
      card.appendChild(hintBanner);
    }

    // 主体：左侧 Tab + 右侧内容
    const body = document.createElement("div");
    body.className = "settings-body";

    // 左侧 Tab 栏
    const sidebar = this._buildSidebar();
    body.appendChild(sidebar);

    // 右侧内容区
    const content = document.createElement("div");
    content.className = "settings-content";
    content.id = "settings-content";
    body.appendChild(content);

    // 底部按钮
    const actions = document.createElement("div");
    actions.className = "settings-actions";

    const cancelBtn = document.createElement("button");
    cancelBtn.className = "dialog-btn dialog-btn--cancel";
    cancelBtn.textContent = "取消";
    cancelBtn.addEventListener("click", () => this._close(null));
    this._cancelBtn = cancelBtn;

    const saveBtn = document.createElement("button");
    saveBtn.className = "dialog-btn dialog-btn--confirm settings-save-btn";
    saveBtn.textContent = this._saveLabel;
    saveBtn.addEventListener("click", () => this._save());
    this._saveBtn = saveBtn;

    actions.appendChild(cancelBtn);
    actions.appendChild(saveBtn);

    card.appendChild(topBar);
    card.appendChild(header);
    card.appendChild(body);
    card.appendChild(actions);
    this._overlay.appendChild(card);

    // 渲染当前 tab
    this._renderTab("model");

    // Escape 关闭
    this._keyHandler = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !this._saving) this._close(null);
      if (e.key === "Tab") this._handleTabTrap(e);
    };
    document.addEventListener("keydown", this._keyHandler);

    // 收集可聚焦元素并设置初始焦点
    requestAnimationFrame(() => {
      this._collectFocusable();
      this._focusFirstElement();
    });
  }

  // ---- 焦点陷阱 ----

  private _collectFocusable(): void {
    this._focusableElements = Array.from(
      this._overlay.querySelectorAll<HTMLElement>(
        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
      )
    ).filter(el => !(el as HTMLButtonElement | HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement).disabled);
  }

  private _focusFirstElement(): void {
    if (this._focusableElements.length > 0) {
      this._focusableElements[0].focus();
      this._focusedIndex = 0;
    }
  }

  private _handleTabTrap(e: KeyboardEvent): void {
    if (this._focusableElements.length === 0) {
      this._collectFocusable();
      if (this._focusableElements.length === 0) return;
    }

    const first = this._focusableElements[0];
    const last = this._focusableElements[this._focusableElements.length - 1];

    if (e.shiftKey) {
      if (document.activeElement === first) {
        e.preventDefault();
        last.focus();
      }
    } else {
      if (document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }
  }

  private _buildSidebar(): HTMLElement {
    const sidebar = document.createElement("div");
    sidebar.className = "settings-sidebar";
    sidebar.setAttribute("role", "tablist");
    sidebar.setAttribute("aria-label", "设置分类");

    for (const tab of TABS) {
      const btn = document.createElement("button");
      btn.className = "settings-tab";
      btn.setAttribute("role", "tab");
      btn.setAttribute("aria-selected", String(tab.id === this._activeTab));
      btn.setAttribute("aria-controls", "settings-content");
      btn.textContent = `${tab.icon} ${tab.label}`;
      btn.addEventListener("click", () => this._switchTab(tab.id));
      if (tab.id === this._activeTab) {
        btn.classList.add("settings-tab--active");
      }
      this._tabBtns.set(tab.id, btn);
      sidebar.appendChild(btn);
    }

    return sidebar;
  }

  private _switchTab(id: TabId): void {
    this._tabBtns.get(this._activeTab)?.classList.remove("settings-tab--active");
    this._tabBtns.get(this._activeTab)?.setAttribute("aria-selected", "false");
    this._activeTab = id;
    const activeBtn = this._tabBtns.get(id);
    activeBtn?.classList.add("settings-tab--active");
    activeBtn?.setAttribute("aria-selected", "true");
    this._renderTab(id);
    this._collectFocusable(); // tab 切换后内容区 DOM 已变，重新收集可聚焦元素
    if (this._focusableElements.length > 0) this._focusableElements[0].focus();
  }

  private _renderTab(id: TabId): void {
    const content = this._overlay.querySelector<HTMLElement>("#settings-content");
    if (!content) return;
    content.innerHTML = "";

    if (id === "model") {
      this._renderModelTabInto(content);
    }

    // Tab 内容区绑定 tabpanel role
    content.setAttribute("role", "tabpanel");
    content.setAttribute("aria-label", TABS.find(t => t.id === id)?.label ?? "");
  }

  // ---- 模型配置 Tab ----

  private _renderModelTab(): void {
    const content = this._overlay.querySelector<HTMLElement>("#settings-content");
    if (!content) return;
    content.innerHTML = "";
    this._renderModelTabInto(content);
  }

  private _renderModelTabInto(parent: HTMLElement): void {
    // 主模型
    parent.appendChild(this._buildSection("主模型", "main"));

    // 心智模型
    const memSection = this._buildSection("心智模型", "mind");

    // 心智总开关
    const toggleRow = document.createElement("div");
    toggleRow.className = "settings-toggle-row";

    const toggleLabel = document.createElement("span");
    toggleLabel.textContent = "启用心智";
    toggleLabel.className = "settings-toggle-label";
    toggleLabel.id = "settings-toggle-label";

    const helpBubble = document.createElement("span");
    helpBubble.className = "help-bubble";
    helpBubble.textContent = "?";
    helpBubble.title = "开启后，风堇将拥有记忆、情绪和羁绊。";

    const toggle = document.createElement("button");
    toggle.className = `toggle-switch ${this._data.mind_enabled ? "toggle-switch--on" : ""}`;
    toggle.setAttribute("role", "switch");
    toggle.setAttribute("aria-checked", String(this._data.mind_enabled));
    toggle.setAttribute("aria-labelledby", "settings-toggle-label");
    const knob = document.createElement("span");
    knob.className = "toggle-switch__knob";
    toggle.appendChild(knob);
    toggle.addEventListener("click", () => {
      this._data.mind_enabled = !this._data.mind_enabled;
      toggle.classList.toggle("toggle-switch--on", this._data.mind_enabled);
      toggle.setAttribute("aria-checked", String(this._data.mind_enabled));
      // 心智 section 整体灰/亮切换（保留 toggle row 可点击）
      memSection.classList.toggle("settings-section--disabled", !this._data.mind_enabled);
      const inputs = memSection.querySelectorAll<HTMLInputElement>("input");
      inputs.forEach((inp) => {
        inp.disabled = !this._data.mind_enabled;
        (inp.parentElement as HTMLElement)?.classList.toggle("settings-field--disabled", !this._data.mind_enabled);
      });
      this._markDirty();
    });

    toggleRow.appendChild(toggleLabel);
    toggleRow.appendChild(helpBubble);
    toggleRow.appendChild(toggle);
    memSection.insertBefore(toggleRow, memSection.firstChild);

    if (!this._data.mind_enabled) {
      memSection.classList.add("settings-section--disabled");
      memSection.querySelectorAll<HTMLInputElement>("input").forEach((inp) => {
        inp.disabled = true;
        (inp.parentElement as HTMLElement)?.classList.add("settings-field--disabled");
      });
    }

    parent.appendChild(memSection);

    // 提示文字
    const note = document.createElement("div");
    note.className = "settings-note";
    note.textContent = "保存后立即生效，无需重启。未修改的字段保持原值。";
    parent.appendChild(note);
  }

  private _buildSection(title: string, sectionKey: "main" | "mind"): HTMLElement {
    const section = document.createElement("div");
    section.className = "settings-section";

    const h = document.createElement("div");
    h.className = "settings-section-title";
    h.textContent = title;
    section.appendChild(h);

    const cfg = this._data[sectionKey] as ModelConfig;
    const fields: { key: keyof ModelConfig; label: string; type: string; hint?: string }[] = [
      { key: "api_key", label: "API Key", type: "password" },
      { key: "base_url", label: "Base URL", type: "text", hint: "仅支持 OpenAI 兼容格式" },
      { key: "model", label: "模型名", type: "text" },
    ];

    for (const f of fields) {
      const row = document.createElement("div");
      row.className = "settings-field";

      const inputId = `settings-${sectionKey}-${f.key}`;

      const label = document.createElement("label");
      label.className = "settings-field-label";
      label.textContent = f.label;
      label.setAttribute("for", inputId);

      const input = document.createElement("input");
      input.className = "settings-field-input";
      input.id = inputId;
      input.type = f.type;
      input.value = cfg[f.key] ?? "";
      input.placeholder = "";
      input.autocomplete = f.type === "password" ? "new-password" : "off";
      // API Key: password 类型默认黑点遮蔽真实值，不设 placeholder
      input.addEventListener("input", () => this._markDirty());

      row.appendChild(label);

      // input 外包一层容器（给密码眼睛按钮提供定位锚点）
      const inputWrap = document.createElement("div");
      inputWrap.className = "settings-field-input-wrap";
      inputWrap.appendChild(input);
      row.appendChild(inputWrap);

      // API Key 眼睛切换（显示/隐藏）
      if (f.type === "password") {
        const eyeBtn = document.createElement("button");
        eyeBtn.className = "eye-btn";
        eyeBtn.setAttribute("type", "button");
        eyeBtn.setAttribute("aria-label", "显示 API Key");
        eyeBtn.innerHTML = EYE_HIDDEN;
        eyeBtn.addEventListener("click", () => {
          const isHidden = input.type === "password";
          input.type = isHidden ? "text" : "password";
          eyeBtn.setAttribute("aria-label", isHidden ? "隐藏 API Key" : "显示 API Key");
          eyeBtn.innerHTML = isHidden ? EYE_VISIBLE : EYE_HIDDEN;
        });
        inputWrap.appendChild(eyeBtn);
      }

      if (f.hint) {
        const hint = document.createElement("div");
        hint.className = "settings-field-hint";
        hint.textContent = f.hint;
        row.appendChild(hint);
      }

      section.appendChild(row);
    }

    return section;
  }

  // ---- 脏跟踪 & 保存 ----

  private _saveBtn!: HTMLButtonElement;
  private _cancelBtn!: HTMLButtonElement;
  private _saving = false;

  setSaving(saving: boolean): void {
    this._saving = saving;
    if (!this._saveBtn || !this._cancelBtn) return;
    this._saveBtn.disabled = saving;
    this._cancelBtn.disabled = saving;
    this._saveBtn.textContent = saving ? "正在保存…" : this._saveLabel;
    this._collectFocusable();
  }

  private _markDirty(): void {
    if (!this._dirty) {
      this._dirty = true;
      if (this._saveBtn) this._saveBtn.style.opacity = "1";
    }
  }

  /** 从 DOM 读取当前值到 _data */
  private _readForm(): void {
    const content = this._overlay.querySelector("#settings-content");
    if (!content) return;
    const sections = content.querySelectorAll<HTMLElement>(".settings-section");
    const keys: (keyof ModelConfig)[] = ["api_key", "base_url", "model"];
    // 第一个 section 是主模型，第二个是心智模型
    for (const [idx, sectionKey] of (["main", "mind"] as const).entries()) {
      const inputs = sections[idx]?.querySelectorAll<HTMLInputElement>("input");
      if (!inputs) continue;
      for (let i = 0; i < keys.length && i < inputs.length; i++) {
        const val = inputs[i].value.trim();
        // API Key 未修改 → null（保持原值）
        if (keys[i] === "api_key" && val === this._originalKeys[sectionKey]) {
          this._data[sectionKey][keys[i]] = null;
          continue;
        }
        // 空值 = null（表示不改）
        this._data[sectionKey][keys[i]] = val || null;
      }
    }
  }

  private async _save(): Promise<void> {
    this._readForm();

    // 无任何修改（含纯心智模型字段保存时 _dirty 已由 _markDirty 设为 true）
    if (!this._dirty) {
      this._close(null); // 无修改则直接关闭面板
      return;
    }

    if (this.onSave) {
      this.setSaving(true);
      try {
        this.onSave(JSON.parse(JSON.stringify(this._data)));
      } catch (error) {
        this.setSaving(false);
        throw error;
      }
      return;
    }

    if (this._resolve) {
      this._resolve(this._data);
      this._resolve = null;
    }
  }

  private _close(result: SettingsData | null, force = false): void {
    if (this._saving && !force) return;
    document.removeEventListener("keydown", this._keyHandler!);
    this._overlay.classList.remove("dialog-overlay--visible");
    setTimeout(() => {
      if (this._overlay.parentElement) {
        document.body.removeChild(this._overlay);
      }
      if (this._resolve) {
        this._resolve(result);
        this._resolve = null;
      }
      // 焦点恢复到触发元素
      if (this._triggerEl) {
        this._triggerEl.focus();
      }
    }, 200);
  }

  private _keyHandler: ((e: KeyboardEvent) => void) | null = null;
}
