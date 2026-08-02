const { chromium } = require("playwright");
const fs = require("fs");

const baseURL = process.env.DISPATCH_TEST_URL || "http://127.0.0.1:18848";
let browser;

async function login(page, profile) {
  await page.goto(baseURL, { waitUntil: "networkidle" });
  await page.locator(`[data-profile="${profile}"]`).click();
  await page.locator(".app-shell").waitFor();
}

async function auditAccessibility(page, label) {
  const issues = await page.evaluate(() => {
    const visible = element => {
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
    };
    const result = [];
    document.querySelectorAll("button").forEach((button, index) => {
      if (visible(button) && !button.textContent.trim() && !button.getAttribute("aria-label")) result.push(`button[${index}] 无可访问名称`);
    });
    document.querySelectorAll("input,select,textarea").forEach((control, index) => {
      if (!visible(control) || control.disabled) return;
      if (!control.name) result.push(`${control.tagName.toLowerCase()}[${index}] 缺少 name`);
      if ((control.matches("input:not([type=file]):not([type=hidden]),textarea")) && !control.hasAttribute("autocomplete")) {
        result.push(`${control.tagName.toLowerCase()}[${index}] 缺少 autocomplete`);
      }
      if (!control.labels?.length && !control.getAttribute("aria-label") && !control.getAttribute("aria-labelledby")) result.push(`${control.tagName.toLowerCase()}[${index}] 缺少标签`);
    });
    document.querySelectorAll('[role="button"]').forEach(element => {
      if (visible(element) && element.tagName !== "BUTTON") result.push(`${element.tagName.toLowerCase()}.${element.className} 用 ARIA 模拟按钮`);
    });
    if (!document.getElementById("mainContent")) result.push("缺少主要内容锚点");
    const visibleHeadings = [...document.querySelectorAll("h1")].filter(visible);
    if (visibleHeadings.length !== 1) result.push(`可见 h1 数量为 ${visibleHeadings.length}`);
    const ids = [...document.querySelectorAll("[id]")].map(element => element.id);
    const duplicates = [...new Set(ids.filter((id, index) => ids.indexOf(id) !== index))];
    if (duplicates.length) result.push(`重复 id：${duplicates.join("、")}`);
    if (/\/(?:span|div|button|p|strong)>/.test(document.body.innerText)) result.push("页面出现疑似未闭合 HTML 文本");
    document.querySelectorAll("[aria-labelledby]").forEach(element => {
      const target = document.getElementById(element.getAttribute("aria-labelledby"));
      if (!target) result.push(`${element.tagName.toLowerCase()} 的 aria-labelledby 指向不存在元素`);
    });
    return result;
  });
  if (issues.length) throw new Error(`${label} 无障碍问题：\n${issues.join("\n")}`);
}

async function auditMobileLayout(page, label) {
  const issues = await page.evaluate(() => {
    const visible = element => {
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
    };
    const result = [];
    const overflow = document.documentElement.scrollWidth - document.documentElement.clientWidth;
    if (overflow > 2) result.push(`页面横向溢出 ${Math.round(overflow)}px`);
    const selectors = "button,a[href],input:not([type=file]):not([type=hidden]):not([type=checkbox]):not([type=radio]),select,textarea,summary,[role=button]";
    document.querySelectorAll(selectors).forEach(element => {
      if (!visible(element) || element.closest(".side") || element.classList.contains("skip-link")) return;
      const rect = element.getBoundingClientRect();
      if (rect.height < 44 || rect.width < 44) {
        const name = element.getAttribute("aria-label") || element.textContent.trim().slice(0, 18) || element.id || element.className;
        result.push(`触控目标「${name}」为 ${Math.round(rect.width)}×${Math.round(rect.height)}`);
      }
    });
    const nav = document.querySelector(".mobile-nav");
    if (!nav || !visible(nav)) result.push("手机底部导航不可见");
    return result;
  });
  if (issues.length) throw new Error(`${label} 手机布局问题：\n${issues.join("\n")}`);

  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
  await page.waitForTimeout(80);
  const overlaps = await page.evaluate(() => {
    const nav = document.querySelector(".mobile-nav");
    if (!nav) return [];
    const navRect = nav.getBoundingClientRect();
    const clippedRect = element => {
      const rect = element.getBoundingClientRect();
      let top = rect.top;
      let bottom = rect.bottom;
      let left = rect.left;
      let right = rect.right;
      for (let parent = element.parentElement; parent && parent !== document.body; parent = parent.parentElement) {
        const style = getComputedStyle(parent);
        const parentRect = parent.getBoundingClientRect();
        if (/(auto|scroll|hidden|clip)/.test(style.overflowY)) {
          top = Math.max(top, parentRect.top);
          bottom = Math.min(bottom, parentRect.bottom);
        }
        if (/(auto|scroll|hidden|clip)/.test(style.overflowX)) {
          left = Math.max(left, parentRect.left);
          right = Math.min(right, parentRect.right);
        }
      }
      return { top, bottom, left, right, width: Math.max(0, right - left), height: Math.max(0, bottom - top) };
    };
    return [...document.querySelectorAll("main button,main a[href],main input,main select,main textarea,main summary")].filter(element => {
      const rect = clippedRect(element);
      const style = getComputedStyle(element);
      return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0 &&
        rect.bottom > navRect.top + 1 && rect.top < navRect.bottom - 1;
    }).map(element => {
      const rect = clippedRect(element);
      const name = element.getAttribute("aria-label") || element.textContent.trim().slice(0, 18) || element.id || element.className;
      return `${name}(${Math.round(rect.top)}–${Math.round(rect.bottom)},导航${Math.round(navRect.top)}–${Math.round(navRect.bottom)})`;
    });
  });
  if (overlaps.length) throw new Error(`${label} 控件被底部导航遮挡：${overlaps.join("、")}`);
  await page.evaluate(() => window.scrollTo(0, 0));
}

async function auditViewportLayout(page, label, expectMobile) {
  const issues = await page.evaluate(expectMobileLayout => {
    const visible = element => {
      if (!element) return false;
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
    };
    const result = [];
    const overflow = document.documentElement.scrollWidth - document.documentElement.clientWidth;
    if (overflow > 2) result.push(`页面横向溢出 ${Math.round(overflow)}px`);
    const mobileVisible = visible(document.querySelector(".mobile-nav"));
    const sideVisible = visible(document.querySelector(".side"));
    if (expectMobileLayout && (!mobileVisible || sideVisible)) result.push("移动端导航形态错误");
    if (!expectMobileLayout && (mobileVisible || !sideVisible)) result.push("桌面端导航形态错误");
    const topbar = document.querySelector(".topbar")?.getBoundingClientRect();
    if (topbar && (topbar.left < -1 || topbar.right > innerWidth + 1)) result.push("顶部栏超出视口");
    return result;
  }, expectMobile);
  if (issues.length) throw new Error(`${label} 响应式布局问题：\n${issues.join("\n")}`);
}

async function assertVisibleFocus(page, selector, label) {
  const locator = page.locator(selector).first();
  await locator.focus();
  const focus = await locator.evaluate(element => {
    const style = getComputedStyle(element);
    return {
      active: document.activeElement === element,
      outlineStyle: style.outlineStyle,
      outlineWidth: parseFloat(style.outlineWidth),
    };
  });
  if (!focus.active || focus.outlineStyle === "none" || focus.outlineWidth < 2) {
    throw new Error(`${label} 缺少清晰键盘焦点：${JSON.stringify(focus)}`);
  }
}

async function auditSemanticContrast(page, label) {
  const issues = await page.evaluate(() => {
    const root = getComputedStyle(document.documentElement);
    const parseColor = value => {
      const color = value.trim().toLowerCase();
      if (color.startsWith("#")) {
        const hex = color.slice(1);
        const normalized = hex.length === 3 ? [...hex].map(part => part + part).join("") : hex;
        return [0, 2, 4].map(index => parseInt(normalized.slice(index, index + 2), 16));
      }
      const rgb = color.match(/^rgba?\(([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)/);
      if (rgb) return rgb.slice(1, 4).map(Number);
      const srgb = color.match(/^color\(srgb\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)/);
      if (srgb) return srgb.slice(1, 4).map(value => Number(value) * 255);
      return null;
    };
    const luminance = rgb => {
      const channels = rgb.map(value => {
        const channel = value / 255;
        return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
      });
      return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
    };
    const contrast = (foreground, background) => {
      const light = Math.max(luminance(foreground), luminance(background));
      const dark = Math.min(luminance(foreground), luminance(background));
      return (light + 0.05) / (dark + 0.05);
    };
    const samples = [
      ["次要文字", "--dim", "--bg"],
      ["主要按钮", "#ffffff", "--accent-fill"],
      ["危险按钮", "#ffffff", "--red-fill"],
      ["成功文字", "--green-text", "--surface"],
      ["提醒文字", "--orange-text", "--surface"],
      ["错误文字", "--red-text", "--surface"],
    ];
    return samples.flatMap(([name, foregroundName, backgroundName]) => {
      const foregroundValue = foregroundName.startsWith("--") ? root.getPropertyValue(foregroundName) : foregroundName;
      const backgroundValue = backgroundName.startsWith("--") ? root.getPropertyValue(backgroundName) : backgroundName;
      const foreground = parseColor(foregroundValue);
      const background = parseColor(backgroundValue);
      if (!foreground || !background) return [`${name}色值无法解析：${foregroundValue}/${backgroundValue}`];
      const ratio = contrast(foreground, background);
      return ratio < 4.5 ? [`${name}对比度仅 ${ratio.toFixed(2)}:1`] : [];
    });
  });
  if (issues.length) throw new Error(`${label} 对比度问题：\n${issues.join("\n")}`);
}

async function traverseRole(page, profile) {
  const navRoot = await page.locator(".mobile-nav").isVisible() ? ".mobile-nav" : ".side .nav";
  const ids = await page.locator(`${navRoot} [data-page]`).evaluateAll(buttons => [...new Set(buttons.map(button => button.dataset.page))]);
  for (const id of ids) {
    await page.locator(`${navRoot} [data-page="${id}"]`).click();
    await page.waitForTimeout(80);
    const urlPage = new URL(page.url()).searchParams.get("page");
    if (urlPage !== id) throw new Error(`${profile}/${id} URL 状态未同步：${page.url()}`);
    await auditAccessibility(page, `${profile}/${id}`);
  }
}

async function auditMobilePages(page, profile, ids) {
  for (const id of ids) {
    await page.evaluate(pageId => {
      requestDirty = false;
      return navigatePage(pageId);
    }, id);
    await page.waitForTimeout(120);
    const urlPage = new URL(page.url()).searchParams.get("page");
    if (urlPage !== id) throw new Error(`${profile}/${id} 手机 URL 状态未同步：${page.url()}`);
    await auditAccessibility(page, `${profile}/mobile/${id}`);
    await auditMobileLayout(page, `${profile}/mobile/${id}`);
    if (profile.endsWith("/320")) process.stdout.write(`${profile}/${id} passed\n`);
  }
}

async function auditMobileMoreNavigation(page, profile) {
  const secondary = await page.evaluate(() => {
    const more = mobileNavFor(navFor(navKey()), navKey()).find(item => item.id === "more");
    return more ? more.secondary.map(item => ({ id: item.id, label: item.label })) : [];
  });
  for (const item of secondary) {
    await page.locator("[data-mobile-more]").click();
    await page.locator(".mobile-more-sheet").waitFor();
    if (await page.locator(".mobile-more-sheet [data-account-action],.mobile-more-sheet [data-more-action]").count()) {
      throw new Error(`${profile} 手机更多菜单混入了账户操作`);
    }
    const entry = page.locator(`[data-more-page="${item.id}"]`);
    if (!await entry.count()) throw new Error(`${profile} 手机更多菜单缺少「${item.label}」`);
    await entry.click();
    await page.waitForFunction(id => new URL(location.href).searchParams.get("page") === id, item.id);
    if (!await page.locator("#mainContent").evaluate(main => document.activeElement === main)) {
      throw new Error(`${profile} 手机更多菜单打开「${item.label}」后主要内容未获得焦点`);
    }
    if (await page.locator("[data-mobile-more]").getAttribute("aria-current") !== "page") {
      throw new Error(`${profile} 手机更多菜单打开「${item.label}」后未标记当前页面`);
    }
    await auditAccessibility(page, `${profile}/mobile/more/${item.id}`);
    await auditMobileLayout(page, `${profile}/mobile/more/${item.id}`);
  }
}

async function auditMobileAccount(page, profile) {
  const account = page.locator("#mobileAccount");
  if (!await account.isVisible()) throw new Error(`${profile} 手机端缺少账户入口`);
  if (await page.locator("#themeToggle").isVisible()) throw new Error(`${profile} 手机顶部工具栏仍重复显示外观按钮`);
  const expected = await page.evaluate(() => ({ name: me.name, role: me.role === "staff" && me.lead ? "组长" : ROLE_LABEL[me.role] || me.role }));
  await account.click();
  await page.locator(".account-sheet").waitFor();
  const accountState = await page.evaluate(() => ({
    name: document.querySelector(".account-sheet .qs-who strong")?.textContent.trim() || "",
    role: document.querySelector(".account-sheet .qs-who span")?.textContent.trim() || "",
    actions: [...document.querySelectorAll(".account-sheet [data-account-action]")].map(button => button.dataset.accountAction),
    focusLabel: document.activeElement?.getAttribute("aria-label") || "",
  }));
  if (accountState.name !== expected.name || !accountState.role.startsWith(expected.role) ||
      accountState.actions.join("|") !== "alerts|theme|refresh|logout" || accountState.focusLabel !== "关闭") {
    throw new Error(`${profile} 手机账户 Sheet 信息或操作不完整：${JSON.stringify(accountState)}`);
  }
  await auditAccessibility(page, `${profile}/mobile/account`);
  await page.locator('[data-account-action="logout"]').click();
  await page.locator(".cfm").waitFor();
  await page.locator(".cfm-cancel").click();
  await page.locator(".cfm").waitFor({ state: "detached" });
  const restored = await page.evaluate(() => ({ dialogs: dialogStack.length, locked: document.body.classList.contains("dialog-open"), app: Boolean(document.querySelector(".app-shell")) }));
  if (restored.dialogs || restored.locked || !restored.app) throw new Error(`${profile} 取消退出后界面状态异常：${JSON.stringify(restored)}`);
}

(async () => {
  process.stdout.write("Launching UI smoke\n");
  const chromePath = process.env.CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
  const launchOptions = { headless: true };
  if (fs.existsSync(chromePath)) launchOptions.executablePath = chromePath;
  else launchOptions.channel = "chrome";
  browser = await chromium.launch(launchOptions);
  const errors = [];

  const adminContext = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const admin = await adminContext.newPage();
  admin.setDefaultTimeout(10000);
  admin.on("pageerror", error => errors.push(`admin pageerror: ${error.message}`));
  admin.on("console", message => {
    if (message.type() === "error") errors.push(`admin console: ${message.text()}`);
  });
  await login(admin, "admin");
  process.stdout.write("Admin logged in\n");
  await auditSemanticContrast(admin, "admin/light");
  const manifestAudit = await (await adminContext.newCDPSession(admin)).send("Page.getAppManifest");
  if (!manifestAudit.data || manifestAudit.errors.length) {
    throw new Error(`Web App Manifest 校验失败：${JSON.stringify(manifestAudit.errors)}`);
  }
  const viewport = await admin.locator('meta[name="viewport"]').getAttribute("content");
  if (/maximum-scale|user-scalable\s*=\s*no/i.test(viewport || "")) throw new Error(`页面禁止缩放：${viewport}`);
  const installExperience = await admin.evaluate(async () => {
    const manifestLink = document.querySelector('link[rel="manifest"]')?.getAttribute("href");
    const touchIcon = document.querySelector('link[rel="apple-touch-icon"]')?.getAttribute("href");
    const manifestResponse = await fetch(manifestLink);
    const manifest = await manifestResponse.json();
    const iconResponses = await Promise.all(manifest.icons.map(async iconEntry => {
      const response = await fetch(iconEntry.src);
      return { status: response.status, type: response.headers.get("content-type"), size: (await response.blob()).size };
    }));
    return {
      manifestLink,
      touchIcon,
      standalone: manifest.display,
      startUrl: manifest.start_url,
      icons: iconResponses,
      capable: document.querySelector('meta[name="apple-mobile-web-app-capable"]')?.content,
      textSizeAdjust: getComputedStyle(document.documentElement).webkitTextSizeAdjust,
    };
  });
  if (installExperience.manifestLink !== "/manifest.webmanifest" || installExperience.touchIcon !== "/app-icon-180.png" ||
      installExperience.standalone !== "standalone" || installExperience.startUrl !== "/" || installExperience.capable !== "yes" ||
      !installExperience.icons.every(iconEntry => iconEntry.status === 200 && iconEntry.type?.startsWith("image/png") && iconEntry.size > 1000) ||
      installExperience.textSizeAdjust !== "100%") {
    throw new Error(`手机主屏模式或文字缩放元信息异常：${JSON.stringify(installExperience)}`);
  }
  const navCurrent = await admin.evaluate(() => ({
    current: page,
    side: [...document.querySelectorAll(".side [aria-current='page']")].map(item => item.dataset.page),
    mobile: [...document.querySelectorAll(".mobile-nav [aria-current='page']")].map(item => item.dataset.page || "more"),
    pageLinks: [...document.querySelectorAll(".side [data-page],.mobile-nav [data-page]")].every(item => item.tagName === "A" && new URL(item.href).searchParams.get("page") === item.dataset.page),
  }));
  if (navCurrent.side.length !== 1 || navCurrent.side[0] !== navCurrent.current || navCurrent.mobile.length !== 1 || !navCurrent.pageLinks) {
    throw new Error(`当前页面的 VoiceOver 导航语义异常：${JSON.stringify(navCurrent)}`);
  }
  const statusSemantics = await admin.evaluate(() => ({
    off: personAvail({ status: "off", shift: "中班", intern: 0 }).label,
    check: STATUS.check.label,
    crewAudit: auditCatOf("编辑users"),
  }));
  if (statusSemantics.off !== "下班" || statusSemantics.check !== "巡检" || statusSemantics.crewAudit !== "crew") {
    throw new Error(`人员状态或审计分类异常：${JSON.stringify(statusSemantics)}`);
  }
  const submitLock = await admin.evaluate(async () => {
    const button = document.createElement("button");
    document.body.appendChild(button);
    let calls = 0;
    let release;
    const pending = new Promise(resolve => { release = resolve; });
    const first = runButtonAction(button, "处理中…", async () => {
      calls += 1;
      await pending;
    });
    const second = runButtonAction(button, "处理中…", async () => { calls += 1; });
    const locked = button.disabled && button.textContent === "处理中…";
    release();
    await Promise.all([first, second]);
    const restored = !button.disabled;
    button.remove();
    return { calls, locked, restored };
  });
  if (submitLock.calls !== 1 || !submitLock.locked || !submitLock.restored) {
    throw new Error(`提交防重复锁异常：${JSON.stringify(submitLock)}`);
  }
  const controlLock = await admin.evaluate(async () => {
    const control = document.createElement("select");
    control.innerHTML = '<option value="test">测试</option>';
    document.body.appendChild(control);
    let calls = 0;
    let failures = 0;
    let rejectAction;
    const pending = new Promise((resolve, reject) => { rejectAction = reject; });
    const first = runControlAction(control, async () => {
      calls += 1;
      await pending;
    }, () => { failures += 1; });
    const second = runControlAction(control, async () => { calls += 1; });
    const locked = control.disabled && control.getAttribute("aria-busy") === "true";
    rejectAction(new Error("模拟保存失败"));
    await Promise.all([first, second]);
    const restored = !control.disabled && !control.hasAttribute("aria-busy") && !control.dataset.busy;
    control.remove();
    return { calls, failures, locked, restored };
  });
  if (controlLock.calls !== 1 || controlLock.failures !== 1 || !controlLock.locked || !controlLock.restored) {
    throw new Error(`即时配置控件防重复或失败恢复异常：${JSON.stringify(controlLock)}`);
  }
  const networkFeedback = await admin.evaluate(async () => {
    const originalFetch = window.fetch;
    const button = document.createElement("button");
    button.innerHTML = "保存";
    document.body.appendChild(button);
    window.fetch = async () => { throw new TypeError("模拟断网"); };
    await runButtonAction(button, "保存中…", () => apiPost("setting", { key: "probe", value: "1" }));
    const result = {
      toast: document.getElementById("toast").textContent,
      disabled: button.disabled,
      label: button.textContent,
      offline: document.querySelector(".app-shell").classList.contains("offline"),
      refreshLabel: document.getElementById("refresh").getAttribute("aria-label"),
    };
    window.fetch = originalFetch;
    setConnectionState(true, false);
    button.remove();
    return result;
  });
  if (!networkFeedback.toast.includes("网络连接失败") || networkFeedback.disabled || networkFeedback.label !== "保存" ||
      !networkFeedback.offline || !networkFeedback.refreshLabel.includes("连接中断")) {
    throw new Error(`弱网提交反馈异常：${JSON.stringify(networkFeedback)}`);
  }
  const timeoutFeedback = await admin.evaluate(async () => {
    const originalFetch = window.fetch;
    const button = document.createElement("button");
    button.textContent = "保存";
    document.body.appendChild(button);
    window.fetch = (resource, options = {}) => new Promise((resolve, reject) => {
      options.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
    });
    await runButtonAction(button, "保存中…", () => apiPost("setting", { key: "probe", value: "1" }, 20));
    const result = {
      toast: document.getElementById("toast").textContent,
      disabled: button.disabled,
      label: button.textContent,
      offline: document.querySelector(".app-shell").classList.contains("offline"),
      refreshLabel: document.getElementById("refresh").getAttribute("aria-label"),
    };
    window.fetch = originalFetch;
    setConnectionState(true, false);
    button.remove();
    return result;
  });
  if (!timeoutFeedback.toast.includes("请求超时") || timeoutFeedback.disabled || timeoutFeedback.label !== "保存" ||
      !timeoutFeedback.offline || !timeoutFeedback.refreshLabel.includes("连接中断")) {
    throw new Error(`请求超时反馈异常：${JSON.stringify(timeoutFeedback)}`);
  }
  await admin.evaluate(async () => {
    statsData = null;
    statsError = "请求超时";
    await navigatePage("stats");
  });
  await admin.locator('[data-retry-load="stats"]').waitFor();
  const statsRetryText = await admin.locator(".load-error").innerText();
  if (!statsRetryText.includes("请求超时") || !statsRetryText.includes("重新加载")) {
    throw new Error(`统计失败重试状态异常：${statsRetryText}`);
  }
  await admin.locator('[data-retry-load="stats"]').click();
  await admin.locator(".metric-row").waitFor();
  await traverseRole(admin, "admin");
  await admin.locator('[data-page="crew"]').first().click();
  await admin.getByText("人员与默认班次", { exact: true }).waitFor();
  if (await admin.getByText("下单账号绑定", { exact: true }).count()) throw new Error("人员与默认班次仍混入账号绑定内容");
  await admin.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await admin.locator('[data-crew-section="roster"]').focus();
  await admin.keyboard.press("ArrowRight");
  await admin.getByText("下单账号绑定", { exact: true }).waitFor();
  await admin.getByText("账号准入与管理层权限", { exact: true }).waitFor();
  const peopleTabSemantics = await admin.evaluate(() => {
    const selected = document.querySelector(".people-mode [aria-selected='true']");
    const panel = document.getElementById("crewPanel");
    return {
      section: selected?.dataset.crewSection,
      focused: document.activeElement?.dataset.crewSection,
      controls: selected?.getAttribute("aria-controls"),
      labelledBy: panel?.getAttribute("aria-labelledby"),
      panelRole: panel?.getAttribute("role"),
    };
  });
  if (peopleTabSemantics.section !== "accounts" || peopleTabSemantics.focused !== "accounts" ||
      peopleTabSemantics.controls !== "crewPanel" || peopleTabSemantics.labelledBy !== "crewTabAccounts" ||
      peopleTabSemantics.panelRole !== "tabpanel") {
    throw new Error(`人员页分段键盘或 VoiceOver 语义异常：${JSON.stringify(peopleTabSemantics)}`);
  }
  await admin.waitForFunction(() => new URL(location.href).searchParams.get("section") === "accounts");
  if (await admin.getByText("人员能力矩阵", { exact: true }).count()) throw new Error("人员能力矩阵仍然显示");
  await admin.keyboard.press("ArrowLeft");
  await admin.getByText("人员与默认班次", { exact: true }).waitFor();
  const today = await admin.evaluate(() => todayStr());
  await admin.locator('[data-leave="test:staff"]').click();
  await admin.locator("#absenceSave").click();
  const absenceValidation = await admin.evaluate(() => ({
    activeId: document.activeElement?.id,
    invalid: document.getElementById("absenceType")?.getAttribute("aria-invalid"),
    issue: document.querySelector("#absenceType")?.closest("label")?.querySelector(".field-issue")?.textContent,
  }));
  if (absenceValidation.activeId !== "absenceType" || absenceValidation.invalid !== "true" || !absenceValidation.issue?.includes("请选择")) {
    throw new Error(`离岗安排错误反馈异常：${JSON.stringify(absenceValidation)}`);
  }
  await admin.locator("#absenceType").selectOption("travel");
  await admin.locator("#absenceStart").fill(today);
  await admin.locator("#absenceEnd").fill(today);
  await admin.locator("#absenceSave").click();
  await admin.getByText("出差安排已保存", { exact: true }).waitFor();
  await admin.locator('[data-leave="test:staff"]').getByText("出差至", { exact: false }).waitFor();
  const shiftOverviewDuringAbsence = await admin.locator(".shift-list").innerText();
  if (shiftOverviewDuringAbsence.includes("测试师傅")) {
    throw new Error("离岗人员仍计入今日班次人员名单");
  }
  await admin.locator('[data-leave="test:staff"]').click();
  await admin.locator("#absenceClear").click();
  await admin.getByText("离岗安排已结束", { exact: true }).waitFor();
  await admin.locator('[data-page="settings"]').first().click();
  await admin.locator('[data-cfgsec="规则库"]').first().click();
  await admin.getByText("团队可预约天数", { exact: true }).waitFor();
  if (await admin.getByText("自动放行(推荐)", { exact: true }).count()) throw new Error("登录准入仍允许自动放行");
  await admin.locator('[data-cfgsec="数据维护"]').first().click();
  await admin.getByText("审计日志永久保留", { exact: true }).waitFor();
  await admin.locator('[data-cfgsec="直播团队"]').first().click();
  await admin.locator("#addTeam").click();
  const teamValidation = await admin.evaluate(() => ({
    activeId: document.activeElement?.id,
    invalid: document.getElementById("newTeam")?.getAttribute("aria-invalid"),
    issue: document.getElementById("newTeam")?.closest(".add-row")?.querySelector(".field-issue")?.textContent,
  }));
  if (teamValidation.activeId !== "newTeam" || teamValidation.invalid !== "true" || !teamValidation.issue?.includes("团队名")) {
    throw new Error(`团队配置错误反馈异常：${JSON.stringify(teamValidation)}`);
  }
  await admin.locator('.side [data-page="request"]').click();
  await admin.locator('[data-when="book"]').click();
  await admin.getByText("预计结束", { exact: true }).waitFor();
  await admin.getByText("预约时长按任务标准工时自动计算，不占用额外时段。", { exact: true }).waitFor();
  if (await admin.locator('[data-rdur], #reqEnd, #qsAt2, #qsDur').count()) {
    throw new Error("预约界面仍存在自定义时长控件");
  }
  await admin.locator("#requestNote").fill("尚未提交的测试草稿");
  await admin.locator('.side [data-page="stats"]').click();
  const draftDialog = admin.locator('[role="dialog"]');
  await draftDialog.waitFor();
  if (await draftDialog.getAttribute("aria-modal") !== "true") throw new Error("草稿确认弹窗缺少 aria-modal");
  if (new URL(admin.url()).searchParams.get("page") !== "request") throw new Error("草稿确认前页面已被错误切换");
  await admin.keyboard.press("Escape");
  await draftDialog.waitFor({ state: "detached" });
  if (!await admin.locator('.side [data-page="stats"]').evaluate(button => document.activeElement === button)) throw new Error("关闭确认弹窗后焦点未恢复");
  await admin.locator('.side [data-page="stats"]').click();
  await admin.locator(".cfm-ok").click();
  await admin.getByText("每日接单趋势", { exact: true }).waitFor();
  if (new URL(admin.url()).searchParams.get("page") !== "stats") throw new Error("确认离开后 URL 未更新");
  await admin.locator("#alertBtn").click();
  const alertDialog = admin.locator('[role="dialog"]');
  await alertDialog.waitFor();
  await admin.keyboard.press("Escape");
  await alertDialog.waitFor({ state: "detached" });
  if (!await admin.locator("#alertBtn").evaluate(button => document.activeElement === button)) throw new Error("提醒设置关闭后焦点未恢复");
  await admin.locator('[data-page="stats"]').first().click();
  await admin.getByText("每日接单趋势", { exact: true }).waitFor();
  await admin.reload({ waitUntil: "networkidle" });
  await admin.getByText("每日接单趋势", { exact: true }).waitFor();
  if (!await admin.locator('.side [data-page="stats"]').evaluate(button => button.classList.contains("active"))) throw new Error("刷新后未恢复 URL 对应页面");
  await admin.locator('[data-range="7d"]').click();
  await admin.waitForFunction(() => new URL(location.href).searchParams.get("range") === "7d");
  await admin.locator('[data-range="month"]').click();
  await admin.waitForFunction(() => new URL(location.href).searchParams.get("range") === "month");
  await admin.evaluate(() => history.back());
  await admin.waitForFunction(() => new URL(location.href).searchParams.get("range") === "7d");
  if (!await admin.locator('[data-range="7d"]').evaluate(button => button.classList.contains("active") && button.getAttribute("aria-pressed") === "true")) {
    throw new Error("浏览器后退没有恢复统计范围或选中语义");
  }
  await admin.reload({ waitUntil: "networkidle" });
  await admin.waitForFunction(() => document.querySelector('[data-range="7d"]')?.classList.contains("active"));
  if (new URL(admin.url()).searchParams.get("range") !== "7d") throw new Error("刷新后统计范围深链丢失");
  await admin.locator('.side [data-page="crew"]').click();
  await admin.getByText("人员与默认班次", { exact: true }).waitFor();
  await admin.locator('.side [data-page="stats"]').click();
  await admin.getByText("每日接单趋势", { exact: true }).waitFor();
  await admin.evaluate(() => history.back());
  await admin.waitForFunction(() => new URL(location.href).searchParams.get("page") === "crew");
  await admin.getByText("人员与默认班次", { exact: true }).waitFor();
  await admin.waitForFunction(() => document.activeElement === document.getElementById("mainContent"));
  await admin.evaluate(() => history.forward());
  await admin.waitForFunction(() => new URL(location.href).searchParams.get("page") === "stats");
  await admin.getByText("每日接单趋势", { exact: true }).waitFor();
  await admin.locator('.side [data-page="request"]').click();
  await admin.locator("#requestNote").fill("浏览器后退保护测试");
  let historyDialogDismissed = false;
  admin.once("dialog", async dialog => {
    historyDialogDismissed = true;
    await dialog.dismiss();
  });
  await admin.evaluate(() => history.back());
  await admin.waitForFunction(() => new URL(location.href).searchParams.get("page") === "request");
  if (!historyDialogDismissed) throw new Error("浏览器后退没有触发草稿保护");
  if (new URL(admin.url()).searchParams.get("page") !== "request") throw new Error("取消浏览器后退后仍离开了下单页");
  if (await admin.locator("#requestNote").inputValue() !== "浏览器后退保护测试") throw new Error("取消浏览器后退后草稿丢失");
  admin.once("dialog", dialog => dialog.accept());
  await admin.evaluate(() => history.back());
  await admin.waitForFunction(() => new URL(location.href).searchParams.get("page") === "stats");
  await admin.getByText("每日接单趋势", { exact: true }).waitFor();
  await admin.screenshot({ path: "/tmp/dispatch-admin-smoke.png", fullPage: true });
  await admin.setViewportSize({ width: 2048, height: 1152 });
  await admin.locator('[data-page="settings"]').first().click();
  const desktopLayout = await admin.evaluate(() => {
    const workspace = document.querySelector(".workspace").getBoundingClientRect();
    const settings = document.querySelector(".settings-wrap").getBoundingClientRect();
    const control = [...document.querySelectorAll(".settings-wrap input,.settings-wrap select")].find(element => element.offsetParent !== null);
    return {
      workspace: Math.round(workspace.width),
      settings: Math.round(settings.width),
      overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      fontSize: parseFloat(getComputedStyle(document.body).fontSize),
      controlHeight: control ? Math.round(control.getBoundingClientRect().height) : 0,
    };
  });
  if (desktopLayout.workspace < 1400 || desktopLayout.settings < 1000) throw new Error(`宽屏内容区过窄：${JSON.stringify(desktopLayout)}`);
  if (desktopLayout.overflow > 2) throw new Error(`电脑端横向溢出 ${desktopLayout.overflow}px`);
  if (desktopLayout.fontSize < 14 || desktopLayout.controlHeight < 34) throw new Error(`电脑端文字或控件过小：${JSON.stringify(desktopLayout)}`);
  await admin.locator('[data-page="crew"]').first().click();
  if (await admin.getByText("已选：通过审核,允许使用", { exact: true }).count()) throw new Error("人员审核按钮仍有错误文案");
  const mobileOrder = await admin.evaluate(async () => {
    const existing = state.orders.find(item =>
      item.assignee_id === "test:staff" &&
      ["assigned", "in_progress", "completion_review"].includes(item.status)
    );
    if (existing) return { status: 200, body: { ok: true, data: existing } };
    const team = state.teams[0];
    const rule = state.sla.find(item => item.grp === "灯光组" && !item.task_type.includes("编歌"));
    let lastResult = null;
    for (let day = 1; day <= 20; day += 1) {
      for (let hour = 15; hour <= 20; hour += 1) {
        const response = await fetch("/api/order", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            team_id: team.id,
            group: "灯光组",
            task: rule.task_type,
            when: "book",
            date: addDaysStr(day),
            at: `${String(hour).padStart(2, "0")}:00`,
            assignee: "test:staff",
          }),
        });
        lastResult = { status: response.status, body: await response.json() };
        if (lastResult.status === 200 && lastResult.body.ok) return lastResult;
      }
    }
    return lastResult;
  });
  if (mobileOrder.status !== 200 || !mobileOrder.body.ok) throw new Error(`手机负责人测试工单创建失败：${JSON.stringify(mobileOrder)}`);
  await admin.evaluate(() => load());
  await admin.setViewportSize({ width: 390, height: 844 });
  await admin.locator('.mobile-nav [data-page="overview"]').click();
  await admin.locator(".schedule-mobile .agenda-summary").waitFor();
  if (await admin.locator(".mobile-nav .nav-btn").count() !== 4) throw new Error("管理员手机底栏不是 4 个主入口");
  if (await admin.locator(".schedule-desktop").isVisible()) throw new Error("手机端仍显示压缩的桌面排班网格");
  const safeAreaCoverage = await admin.evaluate(() => {
    const css = document.querySelector("style").textContent;
    return {
      left: css.includes("safe-area-inset-left"),
      right: css.includes("safe-area-inset-right"),
      top: css.includes("safe-area-inset-top"),
      bottom: css.includes("safe-area-inset-bottom"),
    };
  });
  if (!Object.values(safeAreaCoverage).every(Boolean)) {
    throw new Error(`手机四向安全区覆盖不完整：${JSON.stringify(safeAreaCoverage)}`);
  }
  const toastClearance = await admin.evaluate(() => {
    showToast("移动端状态提示");
    const toastRect = document.getElementById("toast").getBoundingClientRect();
    const navRect = document.querySelector(".mobile-nav").getBoundingClientRect();
    return { toastBottom: toastRect.bottom, navTop: navRect.top };
  });
  if (toastClearance.toastBottom > toastClearance.navTop - 2) {
    throw new Error(`手机提示遮挡底部导航：${JSON.stringify(toastClearance)}`);
  }
  await admin.evaluate(() => navigatePage("request"));
  await admin.locator("#requestNote").waitFor();
  const requestActionClearance = await admin.evaluate(async () => {
    const submit = document.getElementById("placeOrder");
    submit.scrollIntoView({ block: "center" });
    await new Promise(resolve => requestAnimationFrame(resolve));
    const nav = document.querySelector(".mobile-nav");
    const submitRect = submit.getBoundingClientRect();
    const navRect = nav.getBoundingClientRect();
    return {
      position: getComputedStyle(submit).position,
      formOverflow: getComputedStyle(submit.closest(".req-form")).overflow,
      submitTop: Math.round(submitRect.top),
      submitBottom: Math.round(submitRect.bottom),
      navTop: Math.round(navRect.top),
    };
  });
  if (requestActionClearance.position !== "static" || requestActionClearance.formOverflow !== "hidden" ||
      requestActionClearance.submitTop < 58 || requestActionClearance.submitBottom > requestActionClearance.navTop - 8) {
    throw new Error(`手机下单主按钮滚动后仍被顶部或底部导航遮挡：${JSON.stringify(requestActionClearance)}`);
  }
  const keyboardLayout = await admin.evaluate(async () => {
    const note = document.getElementById("requestNote");
    const detected = shouldUseKeyboardLayout(window.innerHeight - 220, window.innerHeight, note);
    mobileViewportBaseline = window.innerHeight + 300;
    note.focus();
    syncMobileViewport(window.innerHeight - 220);
    document.documentElement.style.setProperty("--mobile-viewport-height",`${window.innerHeight - 220}px`);
    const nav = document.querySelector(".mobile-nav");
    const submit = document.getElementById("placeOrder");
    const result = {
      detected,
      active: document.body.classList.contains("keyboard-open"),
      navPointerEvents: getComputedStyle(nav).pointerEvents,
      navOpacity: getComputedStyle(nav).opacity,
      submitPosition: getComputedStyle(submit).position,
      viewportHeight: getComputedStyle(document.documentElement).getPropertyValue("--mobile-viewport-height").trim(),
    };
    note.blur();
    document.body.classList.remove("keyboard-open");
    mobileViewportBaseline = window.innerHeight;
    syncMobileViewport(window.innerHeight);
    return result;
  });
  if (!keyboardLayout.detected || !keyboardLayout.active || keyboardLayout.navPointerEvents !== "none" ||
      keyboardLayout.submitPosition !== "static" || keyboardLayout.viewportHeight !== `${844 - 220}px`) {
    throw new Error(`手机软键盘布局切换异常：${JSON.stringify(keyboardLayout)}`);
  }
  await admin.evaluate(() => {
    state.teams = [];
    requestDirty = false;
    renderApp();
  });
  await admin.getByText("还没有直播团队", { exact: true }).waitFor();
  if (await admin.locator(".req-form,#placeOrder").count() || await admin.locator('[data-request-config="直播团队"]').count() !== 1) {
    throw new Error("管理员缺少直播团队时仍显示可提交的下单表单");
  }
  await auditAccessibility(admin, "admin/mobile/request/empty-teams");
  await auditMobileLayout(admin, "admin/mobile/request/empty-teams");
  await admin.locator('[data-request-config="直播团队"]').click();
  await admin.getByRole("heading", { level: 1, name: "配置" }).waitFor();
  if (await admin.locator("#settingsSectionSelect").inputValue() !== "直播团队") throw new Error("空团队引导未打开直播团队配置");
  await admin.evaluate(() => load());
  await admin.evaluate(async () => {
    await navigatePage("request");
    state.sla = [];
    requestDirty = false;
    renderApp();
  });
  await admin.getByText("还没有可下单的任务", { exact: true }).waitFor();
  if (await admin.locator(".req-form,#placeOrder").count() || await admin.locator('[data-request-config="任务分类与工时"]').count() !== 1) {
    throw new Error("管理员缺少任务规则时仍显示可提交的下单表单");
  }
  await auditAccessibility(admin, "admin/mobile/request/empty-tasks");
  await auditMobileLayout(admin, "admin/mobile/request/empty-tasks");
  await admin.locator('[data-request-config="任务分类与工时"]').click();
  await admin.getByRole("heading", { level: 1, name: "配置" }).waitFor();
  if (await admin.locator("#settingsSectionSelect").inputValue() !== "任务分类与工时") throw new Error("空任务引导未打开任务规则配置");
  await admin.evaluate(() => load());
  await admin.evaluate(() => navigatePage("overview"));
  await admin.locator(".schedule-mobile .agenda-summary").waitFor();
  const mobileOfflineState = await admin.evaluate(() => {
    setConnectionState(false, false);
    const refresh = document.getElementById("refresh");
    const rect = refresh.getBoundingClientRect();
    const result = {
      visible: rect.width > 0 && rect.height > 0 && getComputedStyle(refresh).visibility !== "hidden",
      label: refresh.getAttribute("aria-label"),
      offline: document.querySelector(".app-shell").classList.contains("offline"),
    };
    setConnectionState(true, false);
    return result;
  });
  if (!mobileOfflineState.visible || !mobileOfflineState.offline || !mobileOfflineState.label.includes("连接中断")) {
    throw new Error(`手机端断网状态不可见：${JSON.stringify(mobileOfflineState)}`);
  }
  await auditMobileLayout(admin, "admin/mobile/overview");
  await admin.screenshot({ path: "/tmp/dispatch-admin-mobile-overview.png", fullPage: true });
  await admin.locator('[data-schedview="month"]').click();
  await admin.locator(".schedule-mobile .mobile-range-row.today").waitFor();
  await admin.waitForTimeout(100);
  const monthToday = await admin.evaluate(() => {
    const range = document.querySelector(".schedule-mobile");
    const today = range?.querySelector(".mobile-range-row.today");
    const index = [...(range?.querySelectorAll(".mobile-range-row") || [])].indexOf(today);
    const rangeRect = range?.getBoundingClientRect();
    const todayRect = today?.getBoundingClientRect();
    return { index, scrollTop: range?.scrollTop || 0, visible: Boolean(rangeRect && todayRect && todayRect.top >= rangeRect.top - 1 && todayRect.bottom <= rangeRect.bottom + 1) };
  });
  if (!monthToday.visible || (monthToday.index > 4 && monthToday.scrollTop < 40)) throw new Error(`手机月排班没有自动定位到今天：${JSON.stringify(monthToday)}`);
  await admin.locator('[data-schedview="d0"]').click();
  await admin.locator(".schedule-mobile .agenda-summary").waitFor();
  await admin.locator("[data-schedule-edit]").click();
  await admin.getByText("保存当天排班", { exact: true }).waitFor();
  await admin.waitForFunction(() => document.activeElement?.matches('[data-close][aria-label="关闭"]'));
  await admin.keyboard.press("Escape");
  await admin.evaluate(() => {
    const seed = state.staff[0];
    const extras = Array.from({ length: 24 }, (_, index) => ({
      ...seed,
      open_id: `schedule:dense:${index}`,
      name: `排班成员${String(index + 1).padStart(2, "0")}`,
      lead: 0,
    }));
    state.staff = [...state.staff, ...extras];
    openScheduleEditor(todayStr());
  });
  await admin.locator(".schedule-modal").waitFor();
  await admin.waitForFunction(() => document.activeElement?.matches('[data-close][aria-label="关闭"]'));
  const denseSchedule = await admin.evaluate(() => {
    const modal = document.querySelector(".schedule-modal");
    const form = modal.querySelector(".schedule-form");
    const save = modal.querySelector("#scheduleSave");
    const saveRect = save.getBoundingClientRect();
    return {
      modalOverflow: getComputedStyle(modal).overflowY,
      modalClient: modal.clientHeight,
      modalScroll: modal.scrollHeight,
      formOverflow: getComputedStyle(form).overflowY,
      formClient: form.clientHeight,
      formScroll: form.scrollHeight,
      saveVisible: saveRect.top >= 0 && saveRect.bottom <= innerHeight,
      focusLabel: document.activeElement?.getAttribute("aria-label"),
    };
  });
  if (denseSchedule.modalOverflow !== "hidden" || denseSchedule.modalScroll > denseSchedule.modalClient + 2 ||
      denseSchedule.formOverflow !== "auto" || denseSchedule.formScroll <= denseSchedule.formClient ||
      !denseSchedule.saveVisible || denseSchedule.focusLabel !== "关闭") {
    throw new Error(`手机多人排班没有固定标题与保存操作：${JSON.stringify(denseSchedule)}`);
  }
  await admin.keyboard.press("Escape");
  await admin.evaluate(() => load());
  await admin.locator(".schedule-mobile .agenda-summary").waitFor();
  await admin.locator("[data-mobile-more]").click();
  await admin.getByText("更多功能", { exact: true }).waitFor();
  const mobileSettings = admin.locator(".mobile-more-sheet").getByRole("button", { name: /配置/ });
  await mobileSettings.waitFor();
  await mobileSettings.click();
  await admin.getByRole("heading", { level: 1, name: "配置" }).waitFor();
  if (new URL(admin.url()).searchParams.get("page") !== "settings") throw new Error("手机更多菜单未打开配置页");
  if (!await admin.locator("#mainContent").evaluate(main => document.activeElement === main)) {
    throw new Error("手机更多菜单切页后主要内容未获得焦点");
  }
  if (!await admin.locator("#settingsSectionSelect").isVisible() || await admin.locator(".settings-navigation .adaptive-filter-tabs").isVisible()) {
    throw new Error("手机配置分类没有改为完整可见的单一选择器");
  }
  const settingsSections = await admin.locator("#settingsSectionSelect option").evaluateAll(options => options.map(option => option.value));
  for (const section of settingsSections) {
    await admin.locator("#settingsSectionSelect").selectOption(section);
    await admin.getByRole("heading", { level: 2, name: section, exact: true }).waitFor();
    await auditAccessibility(admin, `admin/mobile/settings/${section}`);
    await auditMobileLayout(admin, `admin/mobile/settings/${section}`);
  }
  await admin.locator("#settingsSectionSelect").selectOption("分组管理");
  const groupManagementLayout = await admin.evaluate(() => {
    const row = document.querySelector(".grp-mrow");
    const controls = [...row.querySelectorAll(".grp-nm,.grp-color,.grp-sort")];
    const rowRect = row.getBoundingClientRect();
    return {
      labels: [...row.querySelectorAll(".grp-field>span")].map(label => label.textContent.trim()),
      controlHeights: controls.map(control => control.getBoundingClientRect().height),
      labelled: controls.every(control => control.labels?.length === 1),
      deleteLabel: row.querySelector("[data-delete=groups]")?.getAttribute("aria-label") || "",
      withinViewport: rowRect.left >= -1 && rowRect.right <= innerWidth + 1,
      addLabel: document.getElementById("addGroup")?.textContent.trim() || "",
    };
  });
  if (groupManagementLayout.labels.join("|") !== "分组名称|组色|顺序" ||
      groupManagementLayout.controlHeights.some(height => height < 44) || !groupManagementLayout.labelled ||
      !groupManagementLayout.deleteLabel.startsWith("删除") || !groupManagementLayout.withinViewport ||
      groupManagementLayout.addLabel !== "添加分组") {
    throw new Error(`手机分组管理字段含义或布局不清晰：${JSON.stringify(groupManagementLayout)}`);
  }
  const temporaryGroup = `回归临时组${Date.now()}`;
  await admin.locator("#newGrp").fill(temporaryGroup);
  await admin.locator("#addGroup").click();
  await admin.waitForFunction(name => state.groups.some(group => group.name === name), temporaryGroup);
  const temporaryGroupId = await admin.evaluate(name => state.groups.find(group => group.name === name).id, temporaryGroup);
  const renamedGroup = `${temporaryGroup}改名`;
  const temporaryGroupName = admin.locator(`[data-entity="groups"][data-id="${temporaryGroupId}"][data-field="name"]`);
  await temporaryGroupName.fill(renamedGroup);
  await temporaryGroupName.press("Tab");
  await admin.waitForFunction(({ id, name }) => state.groups.some(group => Number(group.id) === id && group.name === name), { id: Number(temporaryGroupId), name: renamedGroup });
  await admin.locator(`[data-delete="groups"][data-id="${temporaryGroupId}"]`).click();
  await admin.locator(".cfm-ok").click();
  await admin.waitForFunction(id => !state.groups.some(group => Number(group.id) === id), Number(temporaryGroupId));
  await admin.locator("#settingsSectionSelect").selectOption("规则库");
  await admin.getByRole("heading", { level: 2, name: "规则库" }).waitFor();
  if (new URL(admin.url()).searchParams.get("section") !== "规则库") throw new Error("手机配置分类未写入 URL");
  await admin.reload({ waitUntil: "networkidle" });
  if (await admin.locator("#settingsSectionSelect").inputValue() !== "规则库") throw new Error("刷新后手机配置分类丢失");
  const ruleControlSizing = await admin.evaluate(() => ({
    gateWidth: document.getElementById("gateMode")?.getBoundingClientRect().width || 0,
    numeric: [...document.querySelectorAll("[data-rulenum]")].map(input => ({
      key: input.dataset.rulenum,
      value: input.value,
      width: input.getBoundingClientRect().width,
      height: input.getBoundingClientRect().height,
    })),
  }));
  if (ruleControlSizing.gateWidth < 140 || ruleControlSizing.numeric.some(input => input.width < 80 || input.height < 44)) {
    throw new Error(`手机规则控件被说明文字挤压：${JSON.stringify(ruleControlSizing)}`);
  }
  await admin.locator('.mobile-nav [data-page="dispatch"]').click();
  const mobileAssignee = admin.locator(".mobile-assignee").filter({ hasText: "测试师傅" });
  await mobileAssignee.first().waitFor();
  if (!await mobileAssignee.count()) {
    throw new Error("手机管理端工单未直接显示负责人");
  }
  if (!await admin.locator("#dispatchStatusSelect").isVisible() || await admin.locator(".adaptive-filter-row .adaptive-filter-tabs").isVisible()) {
    throw new Error("手机工单状态仍使用被截断的横向分段器");
  }
  await admin.locator("#dispatchStatusSelect").selectOption("assigned");
  if (new URL(admin.url()).searchParams.get("status") !== "assigned") throw new Error("手机工单状态未写入 URL");
  if (await admin.locator(".order-row .tag").evaluateAll(tags => tags.some(tag => !tag.textContent.includes("已指派")))) {
    throw new Error("手机工单状态选择未正确过滤");
  }
  await admin.locator("#dispatchStatusSelect").selectOption("all");
  await admin.locator("#dispatchSearch").fill("测试师傅");
  await admin.waitForFunction(() => new URL(location.href).searchParams.get("q") === "测试师傅");
  await admin.reload({ waitUntil: "networkidle" });
  await admin.locator("#dispatchSearch").waitFor();
  if (await admin.locator("#dispatchSearch").inputValue() !== "测试师傅") throw new Error("刷新后工单搜索条件丢失");
  await admin.locator("#dispatchSearch").fill("不存在的工单");
  await admin.locator(".dispatch-search-empty").waitFor();
  if (await admin.locator(".order-list .order-row:visible").count()) throw new Error("工单搜索未过滤不匹配结果");
  await admin.locator("#dispatchSearch").fill("");
  await auditMobileLayout(admin, "admin/mobile/dispatch");
  await admin.screenshot({ path: "/tmp/dispatch-admin-mobile-schedule.png", fullPage: true });
  const denseOrderRendering = await admin.evaluate(() => {
    const seed = state.orders[0];
    const orders = Array.from({ length: 60 }, (_, index) => ({ ...seed, id: 900000 + index, status: "assigned" }));
    const host = document.createElement("div");
    host.style.cssText = "position:fixed;left:-10000px;top:0;width:390px";
    host.innerHTML = ordersPanel(orders, "view", "密度验证");
    document.body.appendChild(host);
    const list = host.querySelector(".order-list");
    const contentVisibility = getComputedStyle(host.querySelector(".order-row")).contentVisibility;
    const large = list.classList.contains("large-list");
    host.remove();
    return { large, contentVisibility, regularLarge: document.querySelector(".order-list")?.classList.contains("large-list") };
  });
  if (!denseOrderRendering.large || denseOrderRendering.contentVisibility !== "auto" || denseOrderRendering.regularLarge) {
    throw new Error(`工单渐进渲染没有只用于超大列表：${JSON.stringify(denseOrderRendering)}`);
  }
  await admin.locator('.mobile-nav [data-page="crew"]').click();
  await admin.getByText("人员与默认班次", { exact: true }).waitFor();
  await admin.evaluate(() => {
    const extras = Array.from({ length: 14 }, (_, index) => ({
      open_id: `dense:staff:${index}`,
      name: `密集成员${String(index + 1).padStart(2, "0")}`,
      fs_name: `密集成员${String(index + 1).padStart(2, "0")}`,
      alias: "",
      role: "staff",
      tech_role: index % 5 === 0 ? "学徒" : "灯光师",
      shift: ["早班", "中班", "晚班"][index % 3],
      status: index % 4 === 0 ? "check" : "free",
      task: "",
      grp: "灯光组",
      lead: index === 0 ? 1 : 0,
      intern: index % 5 === 0 ? 1 : 0,
      approved: 1,
      blocked: 0,
      read_only: 0,
      absence_type: "",
      absence_start: "",
      absence_end: "",
    }));
    state.users = [...state.users, ...extras];
    state.staff = [...state.staff, ...extras];
    renderApp();
  });
  await admin.waitForFunction(() => document.querySelectorAll(".crew-disclosure").length >= 14);
  const compactCrew = await admin.evaluate(() => {
    const row = document.querySelector(".crew-tbl .crew-collapsible");
    const hiddenControl = row?.querySelector(".ca-f");
    const disclosure = row?.querySelector(".crew-disclosure");
    return {
      count: document.querySelectorAll(".crew-tbl .crew-collapsible").length,
      height: row?.getBoundingClientRect().height,
      hiddenControl: hiddenControl ? getComputedStyle(hiddenControl).display : "",
      disclosureHeight: disclosure?.getBoundingClientRect().height,
    };
  });
  if (compactCrew.count < 14 || compactCrew.height > 96 || compactCrew.hiddenControl !== "none" || compactCrew.disclosureHeight < 44) {
    throw new Error(`手机高密度人员列表没有保持紧凑可管理：${JSON.stringify(compactCrew)}`);
  }
  await admin.locator("#crewSearch").fill("密集成员07");
  await admin.waitForFunction(() => new URL(location.href).searchParams.get("q") === "密集成员07");
  if (await admin.locator(".crew-tbl .ca-row:visible:not(.ca-head)").count() !== 1) throw new Error("人员搜索未精确过滤长名单");
  await admin.locator("#crewSearch").fill("");
  const firstDisclosure = admin.locator(".crew-disclosure").first();
  await firstDisclosure.click();
  if (await firstDisclosure.getAttribute("aria-expanded") !== "true" || !await admin.locator(".crew-collapsible.expanded .ca-f").first().isVisible()) {
    throw new Error("手机人员管理项无法按需展开");
  }
  await firstDisclosure.click();
  await auditMobileLayout(admin, "admin/mobile/crew");
  await admin.screenshot({ path: "/tmp/dispatch-admin-mobile-crew.png", fullPage: true });
  const crewScroll = await admin.evaluate(() => {
    const target = Math.min(560, Math.max(0, document.documentElement.scrollHeight - innerHeight));
    window.scrollTo(0, target);
    return target;
  });
  if (crewScroll > 80) {
    await admin.locator('.mobile-nav [data-page="dispatch"]').click();
    await admin.waitForTimeout(80);
    const dispatchScroll = await admin.evaluate(() => window.scrollY);
    if (dispatchScroll > 20) throw new Error(`手机切换新页面没有回到该页原位置：${dispatchScroll}`);
    await admin.locator('.mobile-nav [data-page="crew"]').click();
    await admin.waitForTimeout(80);
    const restoredCrewScroll = await admin.evaluate(() => window.scrollY);
    if (Math.abs(restoredCrewScroll - crewScroll) > 24) {
      throw new Error(`手机返回人员页没有恢复滚动位置：${crewScroll}/${restoredCrewScroll}`);
    }
  }
  const dialogScroll = await admin.evaluate(() => {
    const target = Math.min(360, Math.max(0, document.documentElement.scrollHeight - innerHeight));
    window.scrollTo(0, target);
    return window.scrollY;
  });
  if (dialogScroll < 40) throw new Error(`手机人员页不足以验证弹层滚动锁定：${dialogScroll}`);
  await admin.locator("[data-mobile-more]").click();
  await admin.getByText("更多功能", { exact: true }).waitFor();
  const dialogLock = await admin.evaluate(() => ({
    classLocked: document.body.classList.contains("dialog-open"),
    position: getComputedStyle(document.body).position,
    top: parseFloat(document.body.style.top || "0"),
  }));
  if (!dialogLock.classLocked || dialogLock.position !== "fixed" || Math.abs(dialogLock.top + dialogScroll) > 2) {
    throw new Error(`手机弹层未正确锁定背景滚动：${JSON.stringify({ dialogScroll, dialogLock })}`);
  }
  await admin.keyboard.press("Escape");
  await admin.locator(".mobile-more-sheet").waitFor({ state: "detached" });
  const restoredDialogScroll = await admin.evaluate(() => window.scrollY);
  if (Math.abs(restoredDialogScroll - dialogScroll) > 2) {
    throw new Error(`关闭手机弹层后页面跳位：${dialogScroll}/${restoredDialogScroll}`);
  }
  await admin.evaluate(() => load());
  await admin.locator(".crew-tbl").waitFor();
  await auditMobilePages(admin, "admin", ["request", "stats", "settings", "audit"]);
  await admin.evaluate(() => navigatePage("stats"));
  await admin.locator("#statsGroupSelect").waitFor();
  if (!await admin.locator("#statsGroupSelect").isVisible() || await admin.locator(".stats-group-navigation .adaptive-filter-tabs").isVisible()) {
    throw new Error("手机统计分组仍使用被截断的横向分段器");
  }
  await admin.locator("#statsGroupSelect").selectOption("灯光组");
  await admin.waitForFunction(() => new URL(location.href).searchParams.get("group") === "灯光组");
  await admin.reload({ waitUntil: "networkidle" });
  if (await admin.locator("#statsGroupSelect").inputValue() !== "灯光组") throw new Error("刷新后手机统计分组丢失");
  await admin.locator("#statsGroupSelect").selectOption("");
  await admin.evaluate(() => navigatePage("audit"));
  await admin.locator("#auditCategorySelect").waitFor();
  if (!await admin.locator("#auditCategorySelect").isVisible() || await admin.locator(".audit-navigation .adaptive-filter-tabs").isVisible()) {
    throw new Error("手机审计分类没有改为完整可见的单一选择器");
  }
  await admin.locator("#auditCategorySelect").selectOption("order");
  if (new URL(admin.url()).searchParams.get("category") !== "order") throw new Error("手机审计分类未写入 URL");
  await admin.locator("#auditCategorySelect").selectOption("all");
  const denseAudit = await admin.evaluate(() => {
    const makeRows = count => Array.from({ length: count }, (_, index) => ({
      ts: `2026-07-${String((index % 28) + 1).padStart(2, "0")} 12:34:56`,
      name: `审计人员${index}`,
      action: `操作${index}`,
      detail: `第${index}条审计记录包含需要完整展示的业务详情`,
    }));
    auditCat = "all";
    auditRows = makeRows(25);
    renderApp();
    const regularList = document.querySelector(".audit-list");
    const regularRow = regularList?.querySelector(".audit-row");
    const regular = {
      count: regularList?.querySelectorAll(".audit-row").length || 0,
      large: regularList?.classList.contains("large-list") || false,
      contentVisibility: regularRow ? getComputedStyle(regularRow).contentVisibility : "",
    };
    auditRows = makeRows(500);
    renderApp();
    const first = document.querySelector(".audit-row");
    return {
      regular,
      count: document.querySelectorAll(".audit-row").length,
      large: document.querySelector(".audit-list")?.classList.contains("large-list") || false,
      contentVisibility: first ? getComputedStyle(first).contentVisibility : "",
    };
  });
  if (denseAudit.regular.count > 50 || denseAudit.regular.large || denseAudit.regular.contentVisibility === "auto" ||
      denseAudit.count !== 500 || !denseAudit.large || denseAudit.contentVisibility !== "auto") {
    throw new Error(`高密度审计列表没有启用完整渲染优化：${JSON.stringify(denseAudit)}`);
  }
  await auditMobileLayout(admin, "admin/mobile/audit-500");
  await admin.evaluate(() => {
    auditRows = null;
    return load();
  });
  await admin.setViewportSize({ width: 320, height: 700 });
  await auditMobilePages(admin, "admin/320", ["overview", "request", "dispatch", "crew", "stats", "settings", "audit"]);
  await admin.setViewportSize({ width: 768, height: 1024 });
  await admin.evaluate(() => navigatePage("overview"));
  await admin.locator(".schedule-mobile .agenda-summary").waitFor();
  await auditViewportLayout(admin, "admin/tablet/portrait", true);
  await auditMobileLayout(admin, "admin/tablet/portrait");
  await admin.screenshot({ path: "/tmp/dispatch-admin-tablet-portrait.png", fullPage: true });
  await admin.setViewportSize({ width: 1024, height: 768 });
  await auditViewportLayout(admin, "admin/tablet/landscape", false);
  await auditAccessibility(admin, "admin/tablet/landscape");
  await admin.screenshot({ path: "/tmp/dispatch-admin-tablet-landscape.png", fullPage: true });
  await admin.setViewportSize({ width: 1440, height: 1000 });

  const leadContext = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const lead = await leadContext.newPage();
  lead.setDefaultTimeout(10000);
  lead.on("pageerror", error => errors.push(`lead pageerror: ${error.message}`));
  lead.on("console", message => {
    if (message.type() === "error") errors.push(`lead console: ${message.text()}`);
  });
  await login(lead, "lead");
  process.stdout.write("Lead logged in\n");
  await traverseRole(lead, "lead");
  const leadNavText = await lead.locator(".side .nav").innerText();
  if (!leadNavText.includes("本组人员") || leadNavText.includes("配置") || leadNavText.includes("审计")) {
    throw new Error(`组长端导航权限异常：${leadNavText}`);
  }
  await lead.locator('[data-page="crew"]').first().click();
  await lead.getByText("本组人员 · 灯光组", { exact: true }).waitFor();
  if (!await lead.locator('[data-user="test:lead"][data-field="shift"]').isDisabled()) {
    throw new Error("组长仍能修改自己的班次");
  }
  if (!await lead.locator('[data-leave="test:lead"]').isDisabled()) {
    throw new Error("组长仍能修改自己的离岗安排");
  }
  const staffCrewRow = lead.locator('.ca-row:has([data-user="test:staff"])').first();
  await staffCrewRow.locator(".crew-state").click();
  await lead.getByText("人员状态 · 测试师傅", { exact: true }).waitFor();
  await lead.locator('[data-close]').last().click();
  await lead.setViewportSize({ width: 390, height: 844 });
  await lead.locator('.mobile-nav [data-page="overview"]').click();
  await lead.locator(".schedule-mobile .agenda-summary").waitFor();
  await auditMobileLayout(lead, "lead/mobile/overview");
  await lead.screenshot({ path: "/tmp/dispatch-lead-mobile-overview.png", fullPage: true });
  const scheduleTestDate = await lead.evaluate(() => addDaysStr(2));
  await lead.locator('[data-schedview="d2"]').click();
  await lead.locator(".schedule-mobile .agenda-summary").waitFor();
  await lead.locator("[data-schedule-edit]").click();
  const staffSchedule = lead.locator('[data-schedule-worker="test:staff"]');
  await staffSchedule.selectOption("off");
  await lead.locator("#scheduleSave").click();
  await lead.getByText("当天排班已保存，相关人员会收到通知", { exact: true }).waitFor();
  await lead.locator(".schedule-modal").waitFor({ state: "detached" });
  await lead.waitForFunction(date => {
    const item = (state.shift_overrides || []).find(entry => entry.worker_id === "test:staff" && entry.work_date === date);
    return item && Number(item.is_off) === 1;
  }, scheduleTestDate);
  await lead.locator("[data-schedule-edit]").click();
  await lead.locator('[data-schedule-worker="test:staff"]').selectOption("default");
  await lead.locator("#scheduleSave").click();
  await lead.getByText("当天排班已保存，相关人员会收到通知", { exact: true }).waitFor();
  await lead.locator(".schedule-modal").waitFor({ state: "detached" });
  await lead.waitForFunction(date => !(state.shift_overrides || []).some(entry => entry.worker_id === "test:staff" && entry.work_date === date), scheduleTestDate);
  const futurePlanButton = lead.locator('.schedule-mobile [data-plan-person="test:staff"]').first();
  await futurePlanButton.waitFor();
  await futurePlanButton.click();
  if (await lead.locator("#qsDate").inputValue() !== scheduleTestDate) {
    throw new Error("从排班页派单时没有保留当前排班日期");
  }
  const futurePlanInfo = await lead.locator("#qsInfo").innerText();
  if (!futurePlanInfo.includes("预约")) throw new Error(`从排班页打开的派单不是预约模式：${futurePlanInfo}`);
  const quickSubmitLabel = await lead.locator("#qsGo").innerText();
  await lead.locator("#qsAt").fill("");
  await lead.locator("#qsGo").click();
  const quickValidation = await lead.evaluate(() => ({
    activeId: document.activeElement && document.activeElement.id,
    disabled: document.getElementById("qsGo").disabled,
    label: document.getElementById("qsGo").innerText,
    invalid: document.getElementById("qsAt").getAttribute("aria-invalid"),
    issue: document.getElementById("qsAt").closest(".qs-field").querySelector(".field-issue")?.textContent,
  }));
  if (quickValidation.activeId !== "qsAt" || quickValidation.disabled || quickValidation.label !== quickSubmitLabel ||
      quickValidation.invalid !== "true" || !quickValidation.issue?.includes("预约日期和到岗时间")) {
    throw new Error(`快捷派单预约校验状态异常：${JSON.stringify(quickValidation)}`);
  }
  await lead.goBack();
  await lead.locator("#qsheetMask").waitFor({ state: "detached" });
  const dialogBackState = await lead.evaluate(() => ({
    dialogs: dialogStack.length,
    locked: document.body.classList.contains("dialog-open"),
    mask: Boolean(document.querySelector(".qsheet-mask,.modal-mask")),
  }));
  if (dialogBackState.dialogs || dialogBackState.locked || dialogBackState.mask) {
    throw new Error(`手机系统返回后派单弹层仍残留：${JSON.stringify(dialogBackState)}`);
  }
  const customScheduleDate = await lead.evaluate(() => {
    schedView = "day";
    schedDate = addDaysStr(5);
    renderApp();
    return schedDate;
  });
  const customScheduleButton = lead.locator(`.seg [data-day2="${customScheduleDate}"]`);
  await customScheduleButton.waitFor();
  await customScheduleButton.click();
  if (await lead.evaluate(() => schedDate) !== customScheduleDate) {
    throw new Error("点击自定义排班日期后错误跳回今天");
  }
  await lead.setViewportSize({ width: 844, height: 390 });
  await lead.evaluate(() => navigatePage("overview"));
  await lead.locator(".schedule-mobile .agenda-summary").waitFor();
  const landscapeSchedule = await lead.evaluate(() => {
    const schedule = document.querySelector(".schedule-mobile");
    const rangeButtons = [...document.querySelectorAll("[data-schedview]")].filter(button => {
      const rect = button.getBoundingClientRect();
      return rect.width > 0 && rect.height > 0;
    });
    const tops = rangeButtons.map(button => Math.round(button.getBoundingClientRect().top));
    const nav = document.querySelector(".mobile-nav");
    const navButton = nav?.querySelector(".nav-btn");
    return {
      scheduleClientHeight: schedule?.clientHeight || 0,
      scheduleScrollHeight: schedule?.scrollHeight || 0,
      documentClientHeight: document.documentElement.clientHeight,
      documentScrollHeight: document.documentElement.scrollHeight,
      rangeRows: new Set(tops).size,
      navHeight: nav?.getBoundingClientRect().height || 0,
      navButtonHeight: navButton?.getBoundingClientRect().height || 0,
      navButtonDirection: navButton ? getComputedStyle(navButton).flexDirection : "",
    };
  });
  if (landscapeSchedule.scheduleClientHeight <= 0 ||
      landscapeSchedule.documentScrollHeight <= landscapeSchedule.documentClientHeight ||
      landscapeSchedule.rangeRows !== 1 || landscapeSchedule.navHeight > 54 ||
      landscapeSchedule.navButtonHeight > 46 || landscapeSchedule.navButtonDirection !== "row") {
    throw new Error(`组长横屏排班存在不可达内容或范围控件折行：${JSON.stringify(landscapeSchedule)}`);
  }
  await auditMobileLayout(lead, "lead/mobile/overview-landscape");
  await lead.screenshot({ path: "/tmp/dispatch-lead-mobile-landscape.png", fullPage: true });
  await lead.setViewportSize({ width: 390, height: 844 });
  await lead.locator('.mobile-nav [data-page="work"]').click();
  await auditMobileLayout(lead, "lead/mobile/work");
  await auditMobilePages(lead, "lead", ["request", "dispatch", "crew", "stats"]);
  await auditMobileMoreNavigation(lead, "lead");
  await lead.setViewportSize({ width: 320, height: 700 });
  await auditMobilePages(lead, "lead/320", ["overview", "request", "dispatch", "work", "crew", "stats"]);
  await lead.setViewportSize({ width: 1280, height: 900 });

  const staffContext = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const staff = await staffContext.newPage();
  staff.setDefaultTimeout(10000);
  staff.on("pageerror", error => errors.push(`staff pageerror: ${error.message}`));
  staff.on("console", message => {
    if (message.type() === "error") errors.push(`staff console: ${message.text()}`);
  });
  await login(staff, "staff");
  process.stdout.write("Staff logged in\n");
  await traverseRole(staff, "staff");
  const staffNavText = await staff.locator(".side .nav").innerText();
  if (!staffNavText.includes("我的工作") || !staffNavText.includes("给自己下单") || staffNavText.includes("本组人员") || staffNavText.includes("配置") || staffNavText.includes("审计")) {
    throw new Error(`工作人员端导航权限异常：${staffNavText}`);
  }
  await staff.locator('[data-page="request"]').first().click();
  await staff.getByText("固定本人", { exact: true }).waitFor();
  if (await staff.locator("#requestAssignee").inputValue() !== "test:staff") {
    throw new Error("工作人员下单未固定指派给本人");
  }
  await staff.locator('[data-page="work"]').first().click();
  await staff.getByText("状态由组长或管理员维护", { exact: true }).waitFor();
  if (await staff.locator("[data-status]").count()) throw new Error("工作人员端仍存在自改状态控件");
  const statusResponse = await staffContext.request.post(`${baseURL}/api/save`, {
    data: {
      entity: "users",
      record: { open_id: "test:staff", status: "rest" },
    },
  });
  const statusWrite = { status: statusResponse.status(), body: await statusResponse.json() };
  if (statusWrite.status !== 403 || !String(statusWrite.body.msg || "").includes("管理员或组长")) {
    throw new Error(`工作人员自改状态未被服务端拦截：${JSON.stringify(statusWrite)}`);
  }
  const removedStatusResponse = await staffContext.request.post(`${baseURL}/api/tech-status`, {
    data: { status: "rest" },
  });
  const removedStatusRoute = removedStatusResponse.status();
  if (removedStatusRoute !== 404) throw new Error(`旧状态接口仍可访问：${removedStatusRoute}`);
  await staff.setViewportSize({ width: 390, height: 844 });
  await staff.locator('.mobile-nav [data-page="request"]').click();
  await staff.getByText("固定本人", { exact: true }).waitFor();
  if (await staff.locator(".mobile-nav .nav-btn").count() !== 4) throw new Error("工作人员手机底栏不是 4 个主入口");
  await auditMobileLayout(staff, "staff/mobile/request");
  await assertVisibleFocus(staff, "#requestNote", "工作人员下单备注");
  await staff.evaluate(() => {
    state.orders.push({
      id: -900001,
      team: "移动端弹窗测试",
      scheduled_end: new Date(Date.now() + 30 * 60000).toISOString(),
    });
    completeOrder(-900001);
  });
  await staff.waitForFunction(() => document.querySelector('[role="dialog"]')?.contains(document.activeElement));
  await staff.locator("#doneAt").fill("");
  await staff.locator("#doneSubmit").click();
  const doneAtValidation = await staff.evaluate(() => ({
    activeId: document.activeElement?.id,
    invalid: document.getElementById("doneAt")?.getAttribute("aria-invalid"),
    issue: document.getElementById("doneAt")?.closest("label")?.querySelector(".field-issue")?.textContent,
  }));
  if (doneAtValidation.activeId !== "doneAt" || doneAtValidation.invalid !== "true" || !doneAtValidation.issue?.includes("结束时间")) {
    throw new Error(`移动端结束弹窗时间校验异常：${JSON.stringify(doneAtValidation)}`);
  }
  await staff.locator("#doneAt").fill(await staff.evaluate(() => localTimeInput(new Date())));
  await staff.locator("#doneSubmit").click();
  const doneNoteValidation = await staff.evaluate(() => ({
    activeId: document.activeElement?.id,
    invalid: document.getElementById("doneNote")?.getAttribute("aria-invalid"),
    issue: document.getElementById("doneNote")?.closest("label")?.querySelector(".field-issue")?.textContent,
  }));
  if (doneNoteValidation.activeId !== "doneNote" || doneNoteValidation.invalid !== "true" || !doneNoteValidation.issue?.includes("完成说明")) {
    throw new Error(`移动端结束弹窗说明校验异常：${JSON.stringify(doneNoteValidation)}`);
  }
  await staff.locator('[data-close]').last().click();
  await staff.evaluate(() => {
    state.orders = state.orders.filter(order => order.id !== -900001);
    window.__promptProbe = promptBox("校验说明", "请填写具体说明");
  });
  await staff.waitForFunction(() => document.querySelector('[role="dialog"]')?.contains(document.activeElement));
  if (!await staff.locator(".cfm-cancel").evaluate(button => document.activeElement === button)) {
    throw new Error("手机说明弹层没有把初始焦点放在安全的取消按钮");
  }
  await staff.locator(".cfm-ok").click();
  const promptValidation = await staff.evaluate(() => ({
    active: document.activeElement?.classList.contains("cfm-ta"),
    invalid: document.querySelector(".cfm-ta")?.getAttribute("aria-invalid"),
    issue: document.querySelector(".cfm-field .field-issue")?.textContent,
  }));
  if (!promptValidation.active || promptValidation.invalid !== "true" || !promptValidation.issue?.includes("填写说明")) {
    throw new Error(`通用说明弹窗错误反馈异常：${JSON.stringify(promptValidation)}`);
  }
  await staff.keyboard.press("Escape");
  const selfOrderBefore = await staff.evaluate(() => state.orders.map(order => order.id));
  const selfOrderSlot = await staff.evaluate(() => {
    const task = (state.sla || []).find(item => item.grp === me.grp && !item.task_type.includes("编歌"));
    const duration = Number(task?.minutes || 30);
    for (let day = 20; day <= 60; day += 1) {
      const date = addDaysStr(day);
      for (let hour = 15; hour <= 21; hour += 1) {
        const start = new Date(`${date}T${String(hour).padStart(2, "0")}:00:00`).getTime();
        const end = start + duration * 60000;
        const conflict = state.orders.some(order => {
          if (order.assignee_id !== me.open_id || !["assigned", "in_progress", "completion_review"].includes(order.status)) return false;
          const orderStart = Date.parse(String(order.scheduled_start || "").replace(" ", "T"));
          const orderEnd = Date.parse(String(order.scheduled_end || "").replace(" ", "T"));
          return orderStart < end && orderEnd > start;
        });
        if (!conflict) return { date, at: `${String(hour).padStart(2, "0")}:00`, task: task?.task_type || "" };
      }
    }
    return null;
  });
  if (!selfOrderSlot) throw new Error("未找到工作人员自助下单测试时段");
  await staff.locator('[data-when="book"]').click();
  const selfOrderSubmitLabel = await staff.locator("#placeOrder").innerText();
  await staff.locator("#reqDate").fill("");
  await staff.locator("#placeOrder").click();
  const selfOrderValidation = await staff.evaluate(() => ({
    activeId: document.activeElement && document.activeElement.id,
    disabled: document.getElementById("placeOrder").disabled,
    label: document.getElementById("placeOrder").innerText,
    invalid: document.getElementById("reqDate").getAttribute("aria-invalid"),
    issue: document.getElementById("reqDate").closest(".req-sec").querySelector(".field-issue")?.textContent,
  }));
  if (selfOrderValidation.activeId !== "reqDate" || selfOrderValidation.disabled || selfOrderValidation.label !== selfOrderSubmitLabel ||
      selfOrderValidation.invalid !== "true" || !selfOrderValidation.issue?.includes("预约日期和到岗时间")) {
    throw new Error(`工作人员自助下单预约校验状态异常：${JSON.stringify(selfOrderValidation)}`);
  }
  await staff.locator("#requestTask").selectOption({ label: selfOrderSlot.task });
  await staff.locator("#reqDate").fill(selfOrderSlot.date);
  await staff.locator("#reqAt").fill(selfOrderSlot.at);
  await staff.locator("#placeOrder").click();
  await staff.getByText("已下单并派工", { exact: true }).waitFor();
  await staff.waitForFunction(
    before => state.orders.some(order => !before.includes(order.id) && order.creator_id === me.open_id && order.assignee_id === me.open_id),
    selfOrderBefore,
  );
  const createdSelfOrder = await staff.evaluate(before => {
    const order = state.orders.find(item => !before.includes(item.id) && item.creator_id === me.open_id);
    return order ? { id: order.id, assignee_id: order.assignee_id, creator_id: order.creator_id, task: order.task_type } : null;
  }, selfOrderBefore);
  if (!createdSelfOrder || createdSelfOrder.assignee_id !== "test:staff" || createdSelfOrder.creator_id !== "test:staff") {
    throw new Error(`工作人员自助下单闭环异常：${JSON.stringify(createdSelfOrder)}`);
  }
  await staff.evaluate(orderId => reportStart(orderId), createdSelfOrder.id);
  await staff.waitForFunction(() => document.querySelector('[role="dialog"]')?.contains(document.activeElement));
  await staff.locator("#reportStartAt").fill("");
  await staff.locator("#reportStartSubmit").click();
  const reportValidation = await staff.evaluate(() => ({
    activeId: document.activeElement?.id,
    invalid: document.getElementById("reportStartAt")?.getAttribute("aria-invalid"),
    issue: document.getElementById("reportStartAt")?.closest("label")?.querySelector(".field-issue")?.textContent,
  }));
  if (reportValidation.activeId !== "reportStartAt" || reportValidation.invalid !== "true" || !reportValidation.issue?.includes("开始时间")) {
    throw new Error(`移动端补报开始校验异常：${JSON.stringify(reportValidation)}`);
  }
  await staff.locator('[data-close]').last().click();
  await staff.evaluate(orderId => transferOrder(orderId), createdSelfOrder.id);
  await staff.waitForFunction(() => document.querySelector('[role="dialog"]')?.contains(document.activeElement));
  await staff.locator("#transferReason").selectOption("其他紧急情况");
  await staff.locator("#transferSubmit").click();
  const transferValidation = await staff.evaluate(() => ({
    activeId: document.activeElement?.id,
    invalid: document.getElementById("transferDetail")?.getAttribute("aria-invalid"),
    issue: document.getElementById("transferDetail")?.closest("label")?.querySelector(".field-issue")?.textContent,
  }));
  if (transferValidation.activeId !== "transferDetail" || transferValidation.invalid !== "true" || !transferValidation.issue?.includes("具体原因")) {
    throw new Error(`移动端转包说明校验异常：${JSON.stringify(transferValidation)}`);
  }
  await staff.locator('[data-close]').last().click();
  await staff.getByText("我的自助工单", { exact: true }).waitFor();
  await staff.screenshot({ path: "/tmp/dispatch-staff-mobile-request.png", fullPage: true });
  await admin.evaluate(() => load());
  await admin.setViewportSize({ width: 390, height: 844 });
  await admin.evaluate(() => navigatePage("dispatch"));
  const createdOrderRow = admin.locator(`.order-row:has([data-detail][data-oid="${createdSelfOrder.id}"])`);
  await createdOrderRow.waitFor();
  const createdOrderAssignee = await createdOrderRow.locator(".mobile-assignee").innerText();
  if (!createdOrderAssignee.includes("测试师傅")) {
    throw new Error(`管理端手机未显示自助下单负责人：${createdOrderAssignee}`);
  }
  await admin.setViewportSize({ width: 1440, height: 1000 });
  await staff.locator('.mobile-nav [data-page="work"]').click();
  await auditMobileLayout(staff, "staff/mobile/work");
  await auditMobilePages(staff, "staff", ["open", "crewboard", "stats"]);
  await auditMobileMoreNavigation(staff, "staff");
  await staff.setViewportSize({ width: 320, height: 700 });
  await auditMobilePages(staff, "staff/320", ["work", "request", "open", "crewboard", "stats"]);

  const teamContext = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const team = await teamContext.newPage();
  team.setDefaultTimeout(10000);
  team.on("pageerror", error => errors.push(`team pageerror: ${error.message}`));
  team.on("console", message => {
    if (message.type() === "error") errors.push(`team console: ${message.text()}`);
  });
  await login(team, "team");
  process.stdout.write("Team logged in\n");
  await traverseRole(team, "team");
  await auditMobilePages(team, "team", ["request", "orders", "stats"]);
  await team.locator('.mobile-nav [data-page="request"]').click();
  await team.locator('.mobile-nav [data-page="request"]').waitFor();
  const navText = await team.locator(".side .nav").innerText();
  if (navText.includes("人员") || navText.includes("审计")) {
    throw new Error(`团队端出现越权导航：${navText}`);
  }
  await team.setViewportSize({ width: 375, height: 812 });
  await auditMobileLayout(team, "team/mobile/375");
  await auditAccessibility(team, "team/mobile");
  await team.locator('[data-when="book"]').click();
  await auditAccessibility(team, "team/mobile/book");
  await auditMobileLayout(team, "team/mobile/book");
  await team.screenshot({ path: "/tmp/dispatch-team-mobile-smoke.png", fullPage: true });
  await team.setViewportSize({ width: 320, height: 700 });
  await auditViewportLayout(team, "team/mobile/320", true);
  await auditMobilePages(team, "team/320", ["request", "orders", "stats"]);
  await auditMobileAccount(team, "team/320");
  await team.evaluate(() => navigatePage("request"));
  await team.locator("#placeOrder").waitFor();
  await team.screenshot({ path: "/tmp/dispatch-team-mobile-320.png", fullPage: true });
  await team.evaluate(() => {
    state.teams = [];
    requestDirty = false;
    renderApp();
  });
  await team.getByText("当前没有可用的直播团队", { exact: true }).waitFor();
  if (await team.locator(".req-form,#placeOrder,[data-request-config]").count()) {
    throw new Error("团队端缺少绑定团队时仍显示下单表单或管理入口");
  }
  await auditAccessibility(team, "team/mobile/320/empty-team");
  await auditMobileLayout(team, "team/mobile/320/empty-team");
  await team.evaluate(() => load());
  await team.evaluate(() => navigatePage("request"));
  await team.locator("#placeOrder").waitFor();
  const longContent = await team.evaluate(async () => {
    const order = state.orders[0];
    if (!order) return { order: false };
    const token = "超长未分隔字段LongUnbrokenOperationalName".repeat(8);
    if (state.teams[0]) {
      state.teams[0].name = token;
      state.teams[0].room = `${token}直播间`;
    }
    order.team = token;
    order.room = `${token}直播间`;
    order.task_type = `${token}任务`;
    order.assignee_name = `${token}负责人`;
    requestDirty = false;
    await navigatePage("orders");
    return { order: true };
  });
  if (!longContent.order) throw new Error("缺少用于超长业务字段布局测试的工单");
  await team.getByRole("heading", { level: 1, name: "我的工单" }).waitFor();
  await auditViewportLayout(team, "team/mobile/320/long-content", true);
  await auditMobileLayout(team, "team/mobile/320/long-content");
  await team.screenshot({ path: "/tmp/dispatch-team-mobile-320-long-content.png", fullPage: true });
  await team.evaluate(() => load());
  await team.evaluate(() => navigatePage("request"));
  await team.locator("#placeOrder").waitFor();
  await team.setViewportSize({ width: 844, height: 390 });
  await auditMobileLayout(team, "team/mobile/landscape");
  await team.setViewportSize({ width: 390, height: 844 });
  await team.evaluate(() => localStorage.setItem("dispatch_theme", "dark"));
  await team.reload({ waitUntil: "networkidle" });
  await team.locator(".app-shell").waitFor();
  const darkSystemChrome = await team.evaluate(() => ({
    themeColor: document.querySelector('meta[name="theme-color"]')?.content,
    statusBar: document.querySelector('meta[name="apple-mobile-web-app-status-bar-style"]')?.content,
    colorScheme: getComputedStyle(document.documentElement).colorScheme,
  }));
  if (darkSystemChrome.themeColor !== "#1c1c1e" || darkSystemChrome.statusBar !== "black-translucent" || darkSystemChrome.colorScheme !== "dark") {
    throw new Error(`深色模式系统栏未同步：${JSON.stringify(darkSystemChrome)}`);
  }
  await auditSemanticContrast(team, "team/dark");
  await auditMobileLayout(team, "team/mobile/dark");
  await team.screenshot({ path: "/tmp/dispatch-team-mobile-dark.png", fullPage: true });
  await team.emulateMedia({ reducedMotion: "reduce" });
  const transitionDuration = await team.locator(".primary").first().evaluate(element => getComputedStyle(element).transitionDuration);
  if (transitionDuration !== "0s") throw new Error(`减少动态效果未生效：${transitionDuration}`);
  await team.evaluate(() => localStorage.setItem("dispatch_theme", "auto"));
  process.stdout.write("Team mobile audits passed\n");

  await admin.locator('.side [data-page="crew"]').click();
  await admin.locator('[data-crew-section="accounts"]').click();
  await admin.locator('[data-manager-level="readonly"][data-account="test:team"]').click();
  await admin.getByText("已设为管理层只读，可查看全部管理端内容", { exact: true }).waitFor();

  const managerContext = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const manager = await managerContext.newPage();
  manager.setDefaultTimeout(10000);
  await login(manager, "team");
  await manager.getByText("管理层只读", { exact: true }).first().waitFor();
  const managerNav = await manager.locator(".side .nav").innerText();
  for (const label of ["总览", "调度", "人员", "统计", "配置", "审计"]) {
    if (!managerNav.includes(label)) throw new Error(`管理层只读缺少管理端页面：${label}`);
  }
  await manager.locator('.side [data-page="request"]').click();
  if (!await manager.locator("#placeOrder").isDisabled()) throw new Error("管理层只读的下单按钮仍可操作");
  const writeResult = await manager.evaluate(async () => {
    const response = await fetch("/api/setting", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key: "book_team_cap", value: "9" }),
    });
    return { status: response.status, body: await response.json() };
  });
  if (writeResult.status !== 403 || !String(writeResult.body.msg || "").includes("只读")) {
    throw new Error(`管理层只读写接口未拦截：${JSON.stringify(writeResult)}`);
  }
  await manager.screenshot({ path: "/tmp/dispatch-manager-readonly-smoke.png", fullPage: true });
  await managerContext.close();
  process.stdout.write("Manager read-only audit passed\n");

  const teamId = await admin.evaluate(() => state.teams[0].id);
  const restore = await admin.evaluate(async id => {
    const response = await fetch("/api/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        entity: "users",
        record: { open_id: "test:team", role: "team", read_only: "0", team_id: id },
      }),
    });
    return { status: response.status, body: await response.json() };
  }, teamId);
  if (restore.status !== 200 || !restore.body.ok) throw new Error(`测试身份恢复失败：${JSON.stringify(restore)}`);

  process.stdout.write("Closing UI smoke browser\n");
  await browser.close();
  browser = null;
  if (errors.length) throw new Error(errors.join("\n"));
  process.stdout.write("UI smoke passed\n");
})().catch(async error => {
  process.stderr.write(`${error.stack || error}\n`);
  if (browser) await browser.close();
  process.exitCode = 1;
});
