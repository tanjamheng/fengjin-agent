/**
 * CharacterDisplay — 角色展示区（V1：静态图片 + 渐变背景 + 星光粒子）
 *
 * V2 升级路径：容器 DIV 不变，内部替换为 Three.js SceneManager + ModelLoader。
 * 对外接口保持兼容。
 */

export class CharacterDisplay {
  private _container: HTMLElement;
  private _img: HTMLImageElement;
  private _loadingEl: HTMLElement;
  private _particlesContainer: HTMLElement;

  onLoadComplete?: () => void;
  onLoadError?: () => void;

  constructor(container: HTMLElement) {
    this._container = container;
    this._container.classList.add("character-container");

    // 渐变背景在 CSS 中定义

    // 星光粒子容器
    this._particlesContainer = document.createElement("div");
    this._particlesContainer.className = "character-particles";
    this._container.appendChild(this._particlesContainer);

    // 加载提示
    this._loadingEl = document.createElement("div");
    this._loadingEl.className = "character-loading";
    this._loadingEl.textContent = "风堇到来中...";
    this._container.appendChild(this._loadingEl);

    // 角色图片
    this._img = document.createElement("img");
    this._img.className = "character-image";
    this._img.alt = "风堇";
    this._img.style.display = "none";
    this._container.appendChild(this._img);

    this._generateParticles();
  }

  /** 加载角色图片 */
  loadImage(path: string): void {
    this._loadingEl.style.display = "block";
    this._img.style.display = "none";

    this._img.onload = () => {
      this._loadingEl.style.display = "none";
      this._img.style.display = "block";
      this.onLoadComplete?.();
    };

    this._img.onerror = () => {
      this._loadingEl.style.display = "none";
      this._img.style.display = "none";
      // 渐变背景兜底
      this.onLoadError?.();
    };

    this._img.src = path;
  }

  show(): void {
    this._container.style.display = "flex";
  }

  // ---- 私有 ----

  /** 生成 CSS 星光粒子元素（8-12 个） */
  private _generateParticles(): void {
    const count = 8 + Math.floor(Math.random() * 5); // 8-12
    for (let i = 0; i < count; i++) {
      const particle = document.createElement("span");
      particle.className = "character-particle";

      // 随机位置（偏左上区域，30%-70% 范围）
      const left = 10 + Math.random() * 70;
      const top = 5 + Math.random() * 60;
      particle.style.left = `${left}%`;
      particle.style.top = `${top}%`;

      // 随机大小（2-4px）
      const size = 2 + Math.random() * 3;
      particle.style.width = `${size}px`;
      particle.style.height = `${size}px`;

      // 随机 animation-delay
      particle.style.animationDelay = `${Math.random() * 3}s`;

      // 随机 animation-duration (1.5-3s)
      particle.style.animationDuration = `${1.5 + Math.random() * 1.5}s`;

      this._particlesContainer.appendChild(particle);
    }
  }
}
