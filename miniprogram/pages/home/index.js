const app = getApp();
const { roleLabel, statusLabel, managementEntries } = require("../../utils/access");
const { shortDate, shortTime } = require("../../utils/time");

Page({
  data: {
    loading: true,
    errorMessage: "",
    state: null,
    me: null,
    roleText: "",
    metrics: [],
    activeOrders: [],
    management: [],
    onDuty: 0,
  },

  onShow() {
    this.load(true);
  },

  onPullDownRefresh() {
    this.load(true).finally(() => wx.stopPullDownRefresh());
  },

  async load(force) {
    this.setData({ loading: !this.data.state, errorMessage: "" });
    try {
      const state = await app.loadState(force);
      const orders = state.orders || [];
      const activeStatuses = ["pending", "assigned", "in_progress", "completion_review"];
      const activeOrders = orders
        .filter((order) => activeStatuses.includes(order.status))
        .slice(0, 5)
        .map((order) => ({
          ...order,
          statusText: statusLabel(order.status),
          dateText: shortDate(order.date),
          timeText: shortTime(order.arrive || order.scheduled_start),
        }));
      const pending = orders.filter((order) => order.status === "pending").length;
      const running = orders.filter((order) => order.status === "in_progress").length;
      const assigned = orders.filter((order) => order.status === "assigned").length;
      const completed = orders.filter((order) => order.status === "completed").length;
      const onDuty = (state.staff || []).filter((person) => !["off", "leave"].includes(person.status)).length;
      this.setData({
        loading: false,
        state,
        me: state.me,
        roleText: roleLabel(state.me),
        metrics: [
          { key: "pending", label: "待派工", value: pending },
          { key: "assigned", label: "已指派", value: assigned },
          { key: "running", label: "进行中", value: running },
          { key: "completed", label: "今日完成", value: completed },
        ],
        activeOrders,
        management: managementEntries(state.me),
        onDuty,
      });
    } catch (error) {
      if (error.statusCode === 401) return app.showError(error);
      this.setData({ loading: false, errorMessage: error.message || "数据加载失败" });
    }
  },

  navigate(event) {
    const path = event.currentTarget.dataset.path;
    if (path) wx.navigateTo({ url: path });
  },

  openOrder(event) {
    wx.navigateTo({ url: `/pages/orders/index?focus=${event.currentTarget.dataset.id}` });
  },
});
