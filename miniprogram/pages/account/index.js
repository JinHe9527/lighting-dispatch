const app = getApp();
const { roleLabel } = require("../../utils/access");

Page({
  data: {
    loading: true,
    state: null,
    me: null,
    roleText: "",
  },

  onShow() {
    this.load();
  },

  async load() {
    try {
      const state = await app.loadState(true);
      this.setData({
        loading: false,
        state,
        me: { ...state.me, initial: String(state.me.name || "我").slice(0, 1) },
        roleText: roleLabel(state.me),
      });
    } catch (error) {
      app.showError(error);
    }
  },

  copyWebsite() {
    wx.setClipboardData({ data: app.globalData.apiBase, success: () => wx.showToast({ title: "网站地址已复制", icon: "success" }) });
  },

  logout() {
    wx.showModal({
      title: "退出小程序账号",
      content: "只退出当前小程序，不影响网站登录和业务数据。",
      confirmText: "退出",
      success: (result) => {
        if (result.confirm) app.logout();
      },
    });
  },

  unbind() {
    wx.showModal({
      title: "解除微信绑定",
      content: "解除后需要在网站重新生成绑定码才能进入小程序，网站账号和数据不会删除。",
      confirmText: "解除绑定",
      confirmColor: "#A63D31",
      success: async (result) => {
        if (!result.confirm) return;
        try {
          await app.post("/api/wechat/unbind", {});
          app.clearSession();
          wx.reLaunch({ url: "/pages/launch/index" });
        } catch (error) {
          app.showError(error);
        }
      },
    });
  },
});
