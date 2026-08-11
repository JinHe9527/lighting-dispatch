const app = getApp();
const { statusLabel } = require("../../utils/access");
const { shortDate, shortTime } = require("../../utils/time");

const FILTERS = [
  { key: "active", label: "进行中" },
  { key: "pending", label: "待派工" },
  { key: "assigned", label: "已指派" },
  { key: "done", label: "已结束" },
  { key: "all", label: "全部" },
];

Page({
  data: {
    loading: true,
    errorMessage: "",
    state: null,
    me: null,
    filters: FILTERS,
    filter: "active",
    search: "",
    orders: [],
    visibleOrders: [],
    modal: null,
    modalNote: "",
    modalReason: "",
    submitting: false,
  },

  onLoad(options) {
    this.focusOrderId = Number(options.focus || 0);
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
      const orders = (state.orders || []).map((order) => this.decorate(order, state.me));
      this.setData({ loading: false, state, me: state.me, orders }, () => {
        this.applyFilter();
        if (this.focusOrderId) {
          const order = orders.find((item) => item.id === this.focusOrderId);
          if (order) this.setData({ modal: { type: "detail", order } });
          this.focusOrderId = 0;
        }
      });
    } catch (error) {
      if (error.statusCode === 401) return app.showError(error);
      this.setData({ loading: false, errorMessage: error.message || "工单加载失败" });
    }
  },

  decorate(order, me) {
    const manager = me.role === "admin" || (me.role === "staff" && me.lead && me.grp === order.grp);
    const selfAssigned = order.assignee_id === me.open_id;
    return {
      ...order,
      statusText: statusLabel(order.status),
      dateText: shortDate(order.date),
      timeText: shortTime(order.arrive || order.scheduled_start),
      canAssign: order.status === "pending" && me.role === "staff" && me.grp === order.grp,
      canManageAssign: order.status === "pending" && manager,
      canStart: order.status === "assigned" && selfAssigned,
      canComplete: order.status === "in_progress" && selfAssigned,
      canCancel: !["completed", "cancelled", "abnormal"].includes(order.status) && (
        manager || (me.role === "team" && order.creator_id === me.open_id)
      ),
    };
  },

  selectFilter(event) {
    this.setData({ filter: event.currentTarget.dataset.key }, () => this.applyFilter());
  },

  handleSearch(event) {
    this.setData({ search: event.detail.value || "" }, () => this.applyFilter());
  },

  applyFilter() {
    const { filter, search, orders } = this.data;
    const keyword = String(search || "").trim().toLowerCase();
    const visibleOrders = orders.filter((order) => {
      let matchesStatus = true;
      if (filter === "active") matchesStatus = ["pending", "assigned", "in_progress", "completion_review"].includes(order.status);
      if (filter === "pending") matchesStatus = order.status === "pending";
      if (filter === "assigned") matchesStatus = ["assigned", "in_progress", "completion_review"].includes(order.status);
      if (filter === "done") matchesStatus = ["completed", "cancelled", "abnormal"].includes(order.status);
      if (!matchesStatus || !keyword) return matchesStatus;
      return [order.team, order.task_type, order.grp, order.assignee_name, order.note, order.id]
        .some((value) => String(value || "").toLowerCase().includes(keyword));
    });
    this.setData({ visibleOrders });
  },

  showDetail(event) {
    const order = this.data.orders.find((item) => item.id === Number(event.currentTarget.dataset.id));
    if (order) this.setData({ modal: { type: "detail", order }, modalNote: "", modalReason: "" });
  },

  closeModal() {
    if (!this.data.submitting) this.setData({ modal: null, modalNote: "", modalReason: "" });
  },

  stopPropagation() {},

  handleModalNote(event) {
    this.setData({ modalNote: event.detail.value || "" });
  },

  handleModalReason(event) {
    this.setData({ modalReason: event.detail.value || "" });
  },

  async assignOrder(event) {
    const order = this.data.orders.find((item) => item.id === Number(event.currentTarget.dataset.id));
    if (!order) return;
    if (order.canManageAssign) {
      const candidates = (this.data.state.staff || []).filter((person) => person.grp === order.grp && !person.intern);
      const names = candidates.map((person) => person.name);
      wx.showActionSheet({
        itemList: names,
        success: async (result) => {
          const person = candidates[result.tapIndex];
          if (person) await this.runAction("/api/assign", { order_id: order.id, user_id: person.open_id }, `已指派给${person.name}`);
        },
      });
      return;
    }
    await this.runAction("/api/assign", { order_id: order.id }, "接单成功");
  },

  async startOrder(event) {
    await this.runAction("/api/start", { order_id: Number(event.currentTarget.dataset.id) }, "任务已开始");
  },

  showComplete(event) {
    const order = this.data.orders.find((item) => item.id === Number(event.currentTarget.dataset.id));
    if (order) this.setData({ modal: { type: "complete", order }, modalNote: "", modalReason: "" });
  },

  showCancel(event) {
    const order = this.data.orders.find((item) => item.id === Number(event.currentTarget.dataset.id));
    if (order) this.setData({ modal: { type: "cancel", order }, modalNote: "", modalReason: "" });
  },

  async submitModal() {
    const modal = this.data.modal;
    if (!modal || this.data.submitting) return;
    if (modal.type === "complete" && !this.data.modalNote.trim()) {
      wx.showToast({ title: "请填写完成说明", icon: "none" });
      return;
    }
    if (modal.type === "cancel" && !this.data.modalReason.trim()) {
      wx.showToast({ title: "请填写取消原因", icon: "none" });
      return;
    }
    const path = modal.type === "complete" ? "/api/complete" : "/api/cancel-order";
    const data = modal.type === "complete"
      ? { order_id: modal.order.id, note: this.data.modalNote.trim(), reason: this.data.modalReason.trim() }
      : { order_id: modal.order.id, reason: this.data.modalReason.trim() };
    this.setData({ submitting: true });
    try {
      await app.post(path, data);
      wx.showToast({ title: modal.type === "complete" ? "工单已完成" : "工单已取消", icon: "success" });
      this.setData({ modal: null, modalNote: "", modalReason: "", submitting: false });
      await this.load(true);
    } catch (error) {
      this.setData({ submitting: false });
      app.showError(error);
    }
  },

  async runAction(path, data, successMessage) {
    if (this.data.submitting) return;
    this.setData({ submitting: true });
    try {
      await app.post(path, data);
      wx.showToast({ title: successMessage, icon: "success" });
      await this.load(true);
    } catch (error) {
      app.showError(error);
    } finally {
      this.setData({ submitting: false });
    }
  },
});
