App({
  globalData: {
    apiBase: "https://wuyoupaiban.cn",
    state: null,
  },

  loginCode() {
    return new Promise((resolve, reject) => {
      wx.login({
        success(result) {
          if (result.code) {
            resolve(result.code);
            return;
          }
          reject(new Error("微信登录凭证获取失败，请重试"));
        },
        fail() {
          reject(new Error("无法连接微信登录服务，请检查网络后重试"));
        },
      });
    });
  },

  sessionToken() {
    return wx.getStorageSync("dispatch_session_token") || "";
  },

  saveSession(result) {
    const token = result && result.session_token;
    if (!token) throw new Error("服务未返回小程序会话，请重试");
    wx.setStorageSync("dispatch_session_token", token);
    this.globalData.state = null;
  },

  clearSession() {
    wx.removeStorageSync("dispatch_session_token");
    this.globalData.state = null;
  },

  request(path, options = {}) {
    const method = String(options.method || "GET").toUpperCase();
    const token = this.sessionToken();
    const header = { "content-type": "application/json", ...(options.header || {}) };
    if (token) header.Authorization = `Bearer ${token}`;
    return new Promise((resolve, reject) => {
      wx.request({
        url: `${this.globalData.apiBase}${path}`,
        method,
        data: options.data,
        header,
        timeout: options.timeout || 15000,
        success: (result) => {
          const body = result.data && typeof result.data === "object" ? result.data : {};
          if (result.statusCode >= 200 && result.statusCode < 300 && body.ok) {
            resolve(body);
            return;
          }
          if (result.statusCode === 401 && !path.startsWith("/auth/wechat/")) {
            this.clearSession();
          }
          const error = new Error(body.msg || "服务处理失败，请稍后重试");
          error.statusCode = result.statusCode;
          reject(error);
        },
        fail: () => reject(new Error("无法连接派工系统，请检查网络后重试")),
      });
    });
  },

  get(path) {
    return this.request(path);
  },

  post(path, data = {}) {
    return this.request(path, { method: "POST", data });
  },

  async loadState(force = false) {
    if (!force && this.globalData.state) return this.globalData.state;
    const result = await this.get("/api/state");
    this.globalData.state = result.data;
    return result.data;
  },

  uploadFile(filePath) {
    const token = this.sessionToken();
    return new Promise((resolve, reject) => {
      wx.uploadFile({
        url: `${this.globalData.apiBase}/api/upload`,
        filePath,
        name: "file",
        header: token ? { Authorization: `Bearer ${token}` } : {},
        timeout: 60000,
        success: (result) => {
          let body = {};
          try {
            body = JSON.parse(result.data || "{}");
          } catch (error) {
            reject(new Error("上传服务返回异常，请重试"));
            return;
          }
          if (result.statusCode >= 200 && result.statusCode < 300 && body.ok) {
            resolve(body);
            return;
          }
          const uploadError = new Error(body.msg || "附件上传失败");
          uploadError.statusCode = result.statusCode;
          reject(uploadError);
        },
        fail: () => reject(new Error("附件上传失败，请检查网络后重试")),
      });
    });
  },

  downloadMedia(path) {
    const token = this.sessionToken();
    return new Promise((resolve, reject) => {
      wx.downloadFile({
        url: `${this.globalData.apiBase}${path}`,
        header: token ? { Authorization: `Bearer ${token}` } : {},
        timeout: 60000,
        success(result) {
          if (result.statusCode >= 200 && result.statusCode < 300) {
            resolve(result.tempFilePath);
            return;
          }
          reject(new Error("附件下载失败"));
        },
        fail: () => reject(new Error("附件下载失败，请检查网络")),
      });
    });
  },

  async logout() {
    try {
      if (this.sessionToken()) await this.post("/auth/wechat/logout");
    } finally {
      this.clearSession();
      wx.reLaunch({ url: "/pages/launch/index" });
    }
  },

  showError(error) {
    if (error && error.statusCode === 401) {
      this.clearSession();
      wx.reLaunch({ url: "/pages/launch/index" });
      return;
    }
    wx.showToast({ title: (error && error.message) || "操作失败，请重试", icon: "none" });
  },
});
