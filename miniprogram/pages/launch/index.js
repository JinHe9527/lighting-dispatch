const app = getApp();

Page({
  data: {
    phase: "loading",
    bindCode: "",
    submitting: false,
    errorMessage: "",
  },

  onLoad() {
    this.authenticate();
  },

  async authenticate() {
    if (this.data.submitting) return;
    this.setData({ phase: "loading", submitting: true, errorMessage: "" });
    try {
      const code = await app.loginCode();
      const result = await app.post("/auth/wechat/session", { code });
      if (result.bound && result.session_token) {
        app.saveSession(result);
        wx.reLaunch({ url: "/pages/home/index" });
        return;
      }
      app.clearSession();
      this.setData({ phase: "unbound", submitting: false });
    } catch (error) {
      this.setData({
        phase: "error",
        submitting: false,
        errorMessage: error.message || "登录失败，请稍后重试",
      });
    }
  },

  handleBindCode(event) {
    const bindCode = String(event.detail.value || "").replace(/\D/g, "").slice(0, 8);
    this.setData({ bindCode });
  },

  async bindAccount() {
    if (this.data.submitting) return;
    if (!/^\d{8}$/.test(this.data.bindCode)) {
      wx.showToast({ title: "请输入 8 位绑定码", icon: "none" });
      return;
    }
    this.setData({ submitting: true, errorMessage: "" });
    try {
      const code = await app.loginCode();
      const result = await app.post("/auth/wechat/bind", {
        code,
        bind_code: this.data.bindCode,
      });
      if (!result.bound || !result.session_token) {
        throw new Error("微信账号绑定失败，请重试");
      }
      app.saveSession(result);
      wx.reLaunch({ url: "/pages/home/index" });
    } catch (error) {
      this.setData({ submitting: false, errorMessage: error.message || "绑定失败，请重试" });
    }
  },
});
