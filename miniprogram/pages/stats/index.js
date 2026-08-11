const app = getApp();
const { statusLabel } = require("../../utils/access");

function durationText(minutes) {
  const value = Number(minutes || 0);
  if (!value) return "0 分钟";
  if (value < 60) return `${Math.round(value)} 分钟`;
  const hours = Math.floor(value / 60);
  const rest = Math.round(value % 60);
  return rest ? `${hours}时${rest}分` : `${hours} 小时`;
}

Page({
  data: {
    loading: true,
    state: null,
    me: null,
    range: "30d",
    ranges: [
      { key: "7d", label: "7 天" },
      { key: "30d", label: "30 天" },
      { key: "month", label: "本月" },
      { key: "all", label: "全部" },
    ],
    group: "",
    groups: [],
    summaryCards: [],
    byType: [],
    byPerson: [],
    detail: null,
    errorMessage: "",
  },

  onShow() {
    this.initialize();
  },

  onPullDownRefresh() {
    this.loadStats().finally(() => wx.stopPullDownRefresh());
  },

  async initialize() {
    try {
      const state = await app.loadState(false);
      this.setData({ state, me: state.me, groups: state.groups || [] }, () => this.loadStats());
    } catch (error) {
      app.showError(error);
    }
  },

  async loadStats() {
    this.setData({ loading: true, errorMessage: "" });
    try {
      const result = await app.get(`/api/stats?range=${this.data.range}&group=${encodeURIComponent(this.data.group)}`);
      const data = result.data;
      const summary = data.summary || {};
      const maxType = Math.max(1, ...(data.byType || []).map((item) => Number(item.orders) || 0));
      this.setData({
        loading: false,
        summaryCards: [
          { key: "orders", label: "工单总量", value: summary.orders || 0 },
          { key: "completed", label: "已完成", value: summary.completed || 0 },
          { key: "people", label: "参与人员", value: summary.people || 0 },
          { key: "time", label: "实际工时", value: durationText(summary.usedSum) },
        ],
        byType: (data.byType || []).map((item) => ({
          ...item,
          width: `${Math.max(5, Math.round((Number(item.orders) || 0) / maxType * 100))}%`,
          timeText: durationText(item.minutes),
          averageText: item.avgUsed == null ? "暂无实际用时" : `平均 ${durationText(item.avgUsed)}`,
        })),
        byPerson: (data.byPerson || []).map((item) => ({ ...item, timeText: durationText(item.minutes) })),
      });
    } catch (error) {
      if (error.statusCode === 401) return app.showError(error);
      this.setData({ loading: false, errorMessage: error.message || "统计加载失败" });
    }
  },

  selectRange(event) {
    this.setData({ range: event.currentTarget.dataset.range }, () => this.loadStats());
  },

  selectGroup(event) {
    this.setData({ group: event.currentTarget.dataset.group || "" }, () => this.loadStats());
  },

  async openPerson(event) {
    const person = event.currentTarget.dataset.id;
    if (!person) return;
    wx.showLoading({ title: "读取明细" });
    try {
      const result = await app.get(`/api/stats?range=${this.data.range}&group=${encodeURIComponent(this.data.group)}&person=${encodeURIComponent(person)}`);
      const detail = result.data;
      detail.orders = (detail.orders || []).map((order) => ({
        ...order,
        statusText: statusLabel(order.status),
        usedText: order.used == null ? "未计时" : durationText(order.used),
      }));
      this.setData({ detail });
    } catch (error) {
      app.showError(error);
    } finally {
      wx.hideLoading();
    }
  },

  closeDetail() {
    this.setData({ detail: null });
  },

  stopPropagation() {},
});
