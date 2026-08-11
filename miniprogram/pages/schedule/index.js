const app = getApp();
const { dateString, shortTime } = require("../../utils/time");
const { statusLabel } = require("../../utils/access");

function moveDate(value, days) {
  const date = new Date(`${value}T12:00:00`);
  date.setDate(date.getDate() + days);
  return dateString(date);
}

Page({
  data: {
    loading: true,
    me: null,
    state: null,
    range: "day",
    date: dateString(),
    group: "",
    groups: [],
    days: [],
    errorMessage: "",
  },

  onShow() {
    this.initialize();
  },

  onPullDownRefresh() {
    this.loadSchedule().finally(() => wx.stopPullDownRefresh());
  },

  async initialize() {
    try {
      const state = await app.loadState(false);
      if (state.me.role === "team") {
        wx.redirectTo({ url: "/pages/orders/index" });
        return;
      }
      this.setData({ state, me: state.me, groups: state.groups || [] }, () => this.loadSchedule());
    } catch (error) {
      app.showError(error);
    }
  },

  async loadSchedule() {
    this.setData({ loading: true, errorMessage: "" });
    try {
      const result = await app.get(`/api/schedule?range=${this.data.range}&date=${this.data.date}`);
      this.scheduleData = result.data;
      this.buildDays();
    } catch (error) {
      if (error.statusCode === 401) return app.showError(error);
      this.setData({ loading: false, errorMessage: error.message || "排班加载失败" });
    }
  },

  buildDays() {
    const data = this.scheduleData || { assignments: [], orders: [] };
    const assignments = (data.assignments || []).filter((item) => !this.data.group || item.grp === this.data.group);
    const orders = (data.orders || []).filter((item) => !this.data.group || item.grp === this.data.group);
    const dates = [];
    let cursor = data.from;
    while (cursor && cursor <= data.to) {
      dates.push(cursor);
      cursor = moveDate(cursor, 1);
    }
    const days = dates.map((date) => ({
      date,
      shortDate: date.slice(5).replace("-", "/"),
      today: date === data.today,
      assignments: assignments.filter((item) => item.date === date).map((item) => ({
        ...item,
        initial: String(item.worker_name || "人").slice(0, 1),
        shiftText: item.is_off ? "排休" : item.absence_type ? (item.absence_type === "travel" ? "出差" : "请假") : item.name ? `${item.name} ${item.start}–${item.end}` : "未配置班次",
        editable: this.canManage() && item.worker_id !== this.data.me.open_id,
      })),
      orders: orders.filter((item) => item.date === date).map((item) => ({
        ...item,
        timeText: shortTime(item.arrive),
        statusText: statusLabel(item.status),
      })),
    }));
    this.setData({ days, loading: false });
  },

  canManage() {
    const me = this.data.me || {};
    return me.role === "admin" || (me.role === "staff" && me.lead);
  },

  selectRange(event) {
    this.setData({ range: event.currentTarget.dataset.range }, () => this.loadSchedule());
  },

  selectGroup(event) {
    this.setData({ group: event.currentTarget.dataset.group || "" }, () => this.buildDays());
  },

  changeDate(event) {
    this.setData({ date: event.detail.value }, () => this.loadSchedule());
  },

  moveDate(event) {
    const step = Number(event.currentTarget.dataset.step) || 0;
    this.setData({ date: moveDate(this.data.date, step) }, () => this.loadSchedule());
  },

  editAssignment(event) {
    const workerId = event.currentTarget.dataset.worker;
    const workDate = event.currentTarget.dataset.date;
    const assignment = (this.scheduleData.assignments || []).find((item) => item.worker_id === workerId && item.date === workDate);
    if (!assignment || !this.canManage() || workerId === this.data.me.open_id) return;
    const shifts = this.data.state.shifts || [];
    const labels = ["恢复默认班次", "设为排休", ...shifts.map((shift) => `${shift.name} ${shift.start}–${shift.end}`)];
    wx.showActionSheet({
      itemList: labels,
      success: async (result) => {
        let change = { worker_id: workerId, mode: "default" };
        if (result.tapIndex === 1) change = { worker_id: workerId, mode: "off" };
        if (result.tapIndex >= 2) change = { worker_id: workerId, mode: "shift", shift_id: shifts[result.tapIndex - 2].id };
        try {
          await app.post("/api/schedule-adjust", { date: workDate, changes: [change] });
          app.globalData.state = null;
          wx.showToast({ title: "排班已更新", icon: "success" });
          await this.loadSchedule();
        } catch (error) {
          app.showError(error);
        }
      },
    });
  },

  openOrder(event) {
    wx.navigateTo({ url: `/pages/orders/index?focus=${event.currentTarget.dataset.id}` });
  },
});
