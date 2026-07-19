const { chromium } = require("playwright");
const fs = require("fs");

const baseURL = process.env.DISPATCH_TEST_URL || "http://127.0.0.1:18848";

async function login(page, profile) {
  await page.goto(baseURL, { waitUntil: "networkidle" });
  await page.locator(`[data-profile="${profile}"]`).click();
  await page.locator(".app-shell").waitFor();
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
  await admin.locator('[data-page="crew"]').first().click();
  await admin.getByText("下单账号绑定", { exact: true }).waitFor();
  await admin.getByText("人员能力矩阵", { exact: true }).waitFor();
  await admin.locator('[data-page="settings"]').first().click();
  await admin.locator('[data-cfgsec="规则库"]').first().click();
  await admin.getByText("团队可预约天数", { exact: true }).waitFor();
  await admin.locator('[data-cfgsec="数据维护"]').first().click();
  await admin.getByText("审计日志永久保留", { exact: true }).waitFor();
  await admin.locator('.side [data-page="request"]').click();
  await admin.locator('[data-when="book"]').click();
  await admin.getByText("预计结束", { exact: true }).waitFor();
  await admin.getByText("预约时长按任务标准工时自动计算，不占用额外时段。", { exact: true }).waitFor();
  if (await admin.locator('[data-rdur], #reqEnd, #qsAt2, #qsDur').count()) {
    throw new Error("预约界面仍存在自定义时长控件");
  }
  await admin.locator('[data-page="stats"]').first().click();
  await admin.getByText("每日接单趋势", { exact: true }).waitFor();
  await admin.screenshot({ path: "/tmp/dispatch-admin-smoke.png", fullPage: true });

  const leadContext = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const lead = await leadContext.newPage();
  lead.setDefaultTimeout(10000);
  lead.on("pageerror", error => errors.push(`lead pageerror: ${error.message}`));
  lead.on("console", message => {
    if (message.type() === "error") errors.push(`lead console: ${message.text()}`);
  });
  await login(lead, "lead");
  process.stdout.write("Lead logged in\n");
  const leadNavText = await lead.locator(".side .nav").innerText();
  if (!leadNavText.includes("本组人员") || leadNavText.includes("配置") || leadNavText.includes("审计")) {
    throw new Error(`组长端导航权限异常：${leadNavText}`);
  }
  await lead.locator('[data-page="crew"]').first().click();
  await lead.getByText("本组人员 · 灯光组", { exact: true }).waitFor();

  const staffContext = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const staff = await staffContext.newPage();
  staff.setDefaultTimeout(10000);
  staff.on("pageerror", error => errors.push(`staff pageerror: ${error.message}`));
  staff.on("console", message => {
    if (message.type() === "error") errors.push(`staff console: ${message.text()}`);
  });
  await login(staff, "staff");
  process.stdout.write("Staff logged in\n");
  const staffNavText = await staff.locator(".side .nav").innerText();
  if (!staffNavText.includes("我的工作") || staffNavText.includes("人员") || staffNavText.includes("配置") || staffNavText.includes("审计")) {
    throw new Error(`工作人员端导航权限异常：${staffNavText}`);
  }

  const teamContext = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const team = await teamContext.newPage();
  team.setDefaultTimeout(10000);
  team.on("pageerror", error => errors.push(`team pageerror: ${error.message}`));
  team.on("console", message => {
    if (message.type() === "error") errors.push(`team console: ${message.text()}`);
  });
  await login(team, "team");
  process.stdout.write("Team logged in\n");
  await team.locator('.mobile-nav [data-page="request"]').waitFor();
  const navText = await team.locator(".side .nav").innerText();
  if (navText.includes("人员") || navText.includes("审计")) {
    throw new Error(`团队端出现越权导航：${navText}`);
  }
  const overflow = await team.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  if (overflow > 2) throw new Error(`移动端横向溢出 ${overflow}px`);
  await team.screenshot({ path: "/tmp/dispatch-team-mobile-smoke.png", fullPage: true });

  await browser.close();
  if (errors.length) throw new Error(errors.join("\n"));
  process.stdout.write("UI smoke passed\n");
})().catch(error => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
});
