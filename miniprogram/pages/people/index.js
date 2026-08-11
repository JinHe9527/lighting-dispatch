const app = getApp();

const STATUS_LABELS = {
  free: "空闲",
  busy: "忙碌",
  check: "待确认",
  rest: "小休",
  off: "离岗",
};

Page({
  data: {
    loading: true,
    state: null,
    me: null,
    groups: [],
    group: "",
    people: [],
  },

  onShow() {
    this.load(true);
  },

  onPullDownRefresh() {
    this.load(true).finally(() => wx.stopPullDownRefresh());
  },

  async load(force) {
    try {
      const state = await app.loadState(force);
      if (state.me.role === "team") {
        wx.navigateBack();
        return;
      }
      this.setData({ state, me: state.me, groups: state.groups || [], loading: false }, () => this.buildPeople());
    } catch (error) {
      app.showError(error);
    }
  },

  buildPeople() {
    const assignments = this.data.state.today_assignments || [];
    const people = (this.data.state.staff || [])
      .filter((person) => !this.data.group || person.grp === this.data.group)
      .map((person) => {
        const shift = assignments.find((item) => item.worker_id === person.open_id);
        const absence = shift && shift.absence_type;
        return {
          ...person,
          initial: String(person.name || "人").slice(0, 1),
          statusText: absence ? (absence === "travel" ? "出差" : "请假") : (STATUS_LABELS[person.status] || person.status),
          shiftText: shift ? (shift.is_off ? "今日排休" : shift.name ? `${shift.name} ${shift.start}–${shift.end}` : "班次未配置") : person.shift,
          editable: this.canManage(person),
        };
      });
    this.setData({ people });
  },

  canManage(person) {
    const me = this.data.me || {};
    if (person.open_id === me.open_id) return false;
    return me.role === "admin" || (me.role === "staff" && me.lead && me.grp === person.grp);
  },

  selectGroup(event) {
    this.setData({ group: event.currentTarget.dataset.group || "" }, () => this.buildPeople());
  },

  editStatus(event) {
    const person = this.data.people.find((item) => item.open_id === event.currentTarget.dataset.id);
    if (!person || !person.editable) return;
    const values = ["free", "rest", "off"];
    const labels = ["设为空闲", "设为小休", "设为离岗"];
    wx.showActionSheet({
      itemList: labels,
      success: async (result) => {
        try {
          await app.post("/api/save", {
            entity: "users",
            record: { open_id: person.open_id, status: values[result.tapIndex] },
          });
          wx.showToast({ title: "人员状态已更新", icon: "success" });
          await this.load(true);
        } catch (error) {
          app.showError(error);
        }
      },
    });
  },
});
