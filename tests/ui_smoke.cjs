const { chromium } = require("playwright");
const fs = require("fs");

const baseURL = process.env.DISPATCH_TEST_URL || "http://127.0.0.1:18848";

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
      if (!control.labels?.length && !control.getAttribute("aria-label") && !control.getAttribute("aria-labelledby")) result.push(`${control.tagName.toLowerCase()}[${index}] 缺少标签`);
    });
    if (!document.getElementById("mainContent")) result.push("缺少主要内容锚点");
    return result;
  });
  if (issues.length) throw new Error(`${label} 无障碍问题：\n${issues.join("\n")}`);
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

(async () => {
  process.stdout.write("Launching UI smoke\n");
  const chromePath = process.env.CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
  const launchOptions = { headless: true };
  if (fs.existsSync(chromePath)) launchOptions.executablePath = chromePath;
  else launchOptions.channel = "chrome";
  const browser = await chromium.launch(launchOptions);
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
  const viewport = await admin.locator('meta[name="viewport"]').getAttribute("content");
  if (/maximum-scale|user-scalable\s*=\s*no/i.test(viewport || "")) throw new Error(`页面禁止缩放：${viewport}`);
  const statusSemantics = await admin.evaluate(() => ({
    off: personAvail({ status: "off", shift: "中班", intern: 0 }).label,
    check: STATUS.check.label,
    crewAudit: auditCatOf("编辑users"),
  }));
  if (statusSemantics.off !== "下班" || statusSemantics.check !== "巡检" || statusSemantics.crewAudit !== "crew") {
    throw new Error(`人员状态或审计分类异常：${JSON.stringify(statusSemantics)}`);
  }
  await traverseRole(admin, "admin");
  await admin.locator('[data-page="crew"]').first().click();
  await admin.getByText("下单账号绑定", { exact: true }).waitFor();
  await admin.getByText("账号准入与管理层权限", { exact: true }).waitFor();
  if (await admin.getByText("人员能力矩阵", { exact: true }).count()) throw new Error("人员能力矩阵仍然显示");
  const today = await admin.evaluate(() => todayStr());
  await admin.locator('[data-leave="test:staff"]').click();
  await admin.locator("#absenceType").selectOption("travel");
  await admin.locator("#absenceStart").fill(today);
  await admin.locator("#absenceEnd").fill(today);
  await admin.locator("#absenceSave").click();
  await admin.getByText("出差安排已保存", { exact: true }).waitFor();
  await admin.locator('[data-leave="test:staff"]').getByText("出差至", { exact: false }).waitFor();
  await admin.locator('[data-leave="test:staff"]').click();
  await admin.locator("#absenceClear").click();
  await admin.getByText("离岗安排已结束", { exact: true }).waitFor();
  await admin.locator('[data-page="settings"]').first().click();
  await admin.locator('[data-cfgsec="规则库"]').first().click();
  await admin.getByText("团队可预约天数", { exact: true }).waitFor();
  if (await admin.getByText("自动放行(推荐)", { exact: true }).count()) throw new Error("登录准入仍允许自动放行");
  await admin.locator('[data-cfgsec="数据维护"]').first().click();
  await admin.getByText("审计日志永久保留", { exact: true }).waitFor();
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
  if (!staffNavText.includes("我的工作") || staffNavText.includes("人员") || staffNavText.includes("配置") || staffNavText.includes("审计")) {
    throw new Error(`工作人员端导航权限异常：${staffNavText}`);
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
  await team.locator('.mobile-nav [data-page="request"]').click();
  await team.locator('.mobile-nav [data-page="request"]').waitFor();
  const navText = await team.locator(".side .nav").innerText();
  if (navText.includes("人员") || navText.includes("审计")) {
    throw new Error(`团队端出现越权导航：${navText}`);
  }
  const overflow = await team.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  if (overflow > 2) throw new Error(`移动端横向溢出 ${overflow}px`);
  const smallTargets = await team.evaluate(() => {
    const selectors = ".top-actions .icon-btn,.mobile-nav .nav-btn,.req-form .filter,.req-form .chip,.req-form select,.req-form input:not(.file-over),.req-form textarea";
    return [...document.querySelectorAll(selectors)].filter(element => {
      const rect = element.getBoundingClientRect(), style = getComputedStyle(element);
      return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0 && (rect.height < 40 || (["BUTTON","INPUT","SELECT"].includes(element.tagName) && rect.width < 40));
    }).map(element => `${element.tagName}.${element.className}=${Math.round(element.getBoundingClientRect().width)}x${Math.round(element.getBoundingClientRect().height)}`);
  });
  if (smallTargets.length) throw new Error(`移动端触控目标过小：${smallTargets.join(", ")}`);
  await auditAccessibility(team, "team/mobile");
  await team.locator('[data-when="book"]').click();
  await auditAccessibility(team, "team/mobile/book");
  const mobileOverflow = await team.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  if (mobileOverflow > 2) throw new Error(`移动预约页横向溢出 ${mobileOverflow}px`);
  await team.screenshot({ path: "/tmp/dispatch-team-mobile-smoke.png", fullPage: true });

  await admin.locator('.side [data-page="crew"]').click();
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

  await browser.close();
  if (errors.length) throw new Error(errors.join("\n"));
  process.stdout.write("UI smoke passed\n");
})().catch(error => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
});
