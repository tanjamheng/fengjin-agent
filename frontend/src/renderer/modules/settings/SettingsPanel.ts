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

export interface ModelConfig {
  api_key: string | null;  // null = 不改
  base_url: string | null;
  model: string | null;
}

export interface SettingsData {
  main: ModelConfig;
  memory: ModelConfig;
  memory_enabled: boolean;
}

export class SettingsPanel {
  private _overlay!: HTMLElement;
  private _activeTab: TabId = "model";
  private _data: SettingsData;
  private _original: SettingsData;  // 快照，用于取消恢复
  private _resolve: ((result: SettingsData | null) => void) | null = null;

  // 脏跟踪
  private _dirty = false;

  // Tab 按钮引用
  private _tabBtns = new Map<TabId, HTMLElement>();

  constructor(initialData: SettingsData) {
    this._data = JSON.parse(JSON.stringify(initialData));
    this._original = JSON.parse(JSON.stringify(initialData));
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

  /** 更新内存中的数据（get_config 返回后调用） */
  updateData(data: SettingsData): void {
    this._data = JSON.parse(JSON.stringify(data));
    this._original = JSON.parse(JSON.stringify(data));
    this._dirty = false;
    this._renderModelTab();
  }

  // ---- 构建 ----

  private _build(): void {
    this._overlay = document.createElement("div");
    this._overlay.className = "dialog-overlay settings-overlay";
    this._overlay.addEventListener("click", (e) => {
      if (e.target === this._overlay) this._close(null);
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
    header.appendChild(h);

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

    const saveBtn = document.createElement("button");
    saveBtn.className = "dialog-btn dialog-btn--confirm settings-save-btn";
    saveBtn.textContent = "保存并应用";
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
      if (e.key === "Escape") this._close(null);
    };
    document.addEventListener("keydown", this._keyHandler);
  }

  private _buildSidebar(): HTMLElement {
    const sidebar = document.createElement("div");
    sidebar.className = "settings-sidebar";

    for (const tab of TABS) {
      const btn = document.createElement("button");
      btn.className = "settings-tab";
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
    this._activeTab = id;
    this._tabBtns.get(id)?.classList.add("settings-tab--active");
    this._renderTab(id);
  }

  private _renderTab(id: TabId): void {
    const content = this._overlay.querySelector("#settings-content");
    if (!content) return;
    content.innerHTML = "";

    if (id === "model") {
      this._renderModelTabInto(content);
    }
  }

  // ---- 模型配置 Tab ----

  private _renderModelTab(): void {
    const content = this._overlay.querySelector("#settings-content");
    if (!content) return;
    content.innerHTML = "";
    this._renderModelTabInto(content);
  }

  private _renderModelTabInto(parent: HTMLElement): void {
    // 主模型
    parent.appendChild(this._buildSection("主模型", "main"));

    // 记忆模型
    const memSection = this._buildSection("记忆模型", "memory");

    // 记忆开关
    const toggleRow = document.createElement("div");
    toggleRow.className = "settings-toggle-row";

    const toggleLabel = document.createElement("span");
    toggleLabel.textContent = "启用记忆";
    toggleLabel.className = "settings-toggle-label";

    const toggle = document.createElement("button");
    toggle.className = `settings-toggle ${this._data.memory_enabled ? "settings-toggle--on" : ""}`;
    toggle.textContent = this._data.memory_enabled ? "开" : "关";
    toggle.addEventListener("click", () => {
      this._data.memory_enabled = !this._data.memory_enabled;
      toggle.classList.toggle("settings-toggle--on", this._data.memory_enabled);
      toggle.textContent = this._data.memory_enabled ? "开" : "关";
      // 灰色不可编辑的视觉
      const inputs = memSection.querySelectorAll<HTMLInputElement>("input");
      inputs.forEach((inp) => {
        inp.disabled = !this._data.memory_enabled;
        (inp.parentElement as HTMLElement)?.classList.toggle("settings-field--disabled", !this._data.memory_enabled);
      });
      this._markDirty();
    });

    toggleRow.appendChild(toggleLabel);
    toggleRow.appendChild(toggle);
    memSection.insertBefore(toggleRow, memSection.firstChild);

    if (!this._data.memory_enabled) {
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

  private _buildSection(title: string, sectionKey: "main" | "memory"): HTMLElement {
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

      const label = document.createElement("label");
      label.className = "settings-field-label";
      label.textContent = f.label;

      const input = document.createElement("input");
      input.className = "settings-field-input";
      input.type = f.type;
      input.value = cfg[f.key] ?? "";
      input.placeholder = f.type === "password" ? "输入以更新" : "";
      // API Key 脱敏显示
      if (f.key === "api_key" && cfg[f.key] && cfg[f.key]!.startsWith("****")) {
        input.value = "";  // 不显示脱敏后的值，留空让用户重新输入
        input.placeholder = "输入新的 API Key（留空则不变）";
      }
      input.addEventListener("input", () => this._markDirty());

      row.appendChild(label);
      row.appendChild(input);

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
    // 第一个 section 是主模型，第二个是记忆模型
    for (const [idx, sectionKey] of (["main", "memory"] as const).entries()) {
      const inputs = sections[idx]?.querySelectorAll<HTMLInputElement>("input");
      if (!inputs) continue;
      for (let i = 0; i < keys.length && i < inputs.length; i++) {
        const val = inputs[i].value.trim();
        // 空值 = null（表示不改）
        this._data[sectionKey][keys[i]] = val || null;
      }
    }
  }

  private async _save(): Promise<void> {
    this._readForm();

    // 必填校验：主模型的三个字段，如果原来就是空的且没填，则报错
    const mainEmpty = !this._data.main.api_key && !this._data.main.base_url && !this._data.main.model;
    if (mainEmpty) {
      return;  // 全空说明用户没有填任何东西，忽略
    }

    if (this._resolve) {
      this._resolve(this._data);
      this._resolve = null;
    }
  }

  private _close(result: SettingsData | null): void {
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
    }, 200);
  }

  private _keyHandler: ((e: KeyboardEvent) => void) | null = null;
}
