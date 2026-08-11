const app = getApp();

const TABS = [
  { key: "accounts", label: "账号" },
  { key: "teams", label: "团队" },
  { key: "sla", label: "任务" },
  { key: "shifts", label: "班次" },
  { key: "groups", label: "分组" },
  { key: "rules", label: "规则" },
];

const ACCOUNT_TYPES = [
  { key: "team", label: "团队账号" },
  { key: "intern", label: "实习成员" },
  { key: "staff", label: "正式成员" },
  { key: "lead", label: "组长" },
  { key: "readonly", label: "管理只读" },
  { key: "admin", label: "管理员" },
];

const ACCESS_TYPES = [
  { key: "pending", label: "待审核" },
  { key: "approved", label: "允许访问" },
  { key: "blocked", label: "禁止访问" },
];

Page({
  data: {
    loading: true,
    state: null,
    me: null,
    tabs: TABS,
    tab: "accounts",
    list: [],
    modal: null,
    form: {},
    accountTypes: ACCOUNT_TYPES,
    accessTypes: ACCESS_TYPES,
    accountTypeIndex: 2,
    accessIndex: 0,
    groupIndex: 0,
    teamIndex: 0,
    submitting: false,
    ruleItems: [],
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
      if (state.me.role !== "admin") {
        wx.navigateBack();
        return;
      }
      this.setData({ state, me: state.me, loading: false }, () => this.buildList());
    } catch (error) {
      app.showError(error);
    }
  },

  selectTab(event) {
    this.setData({ tab: event.currentTarget.dataset.tab }, () => this.buildList());
  },

  buildList() {
    const state = this.data.state;
    const tab = this.data.tab;
    let list = [];
    if (tab === "accounts") {
      list = (state.users || []).map((user) => ({
        ...user,
        typeText: this.accountType(user).label,
        accessText: user.blocked ? "禁止访问" : user.approved ? "允许访问" : "待审核",
        metaText: user.role === "team"
          ? ((state.teams || []).find((team) => team.id === user.team_id) || {}).name || "未绑定团队"
          : user.role === "staff" ? `${user.grp || '未分组'} · ${user.shift || '未排班'}` : "系统管理",
      }));
    } else if (tab === "teams") list = state.teams || [];
    else if (tab === "sla") list = state.sla || [];
    else if (tab === "shifts") list = state.shifts || [];
    else if (tab === "groups") list = state.groups || [];
    if (tab === "rules") this.buildRules();
    this.setData({ list });
  },

  accountType(user) {
    let key = user.role;
    if (user.role === "staff") key = user.intern ? "intern" : user.lead ? "lead" : "staff";
    if (user.role === "admin" && user.read_only) key = "readonly";
    return ACCOUNT_TYPES.find((item) => item.key === key) || ACCOUNT_TYPES[2];
  },

  buildRules() {
    const rules = this.data.state.rules || {};
    const reminder = this.data.state.reminder || {};
    const items = [
      { key: "book_team_cap", label: "团队未开始预约上限", value: rules.team_cap || "2", suffix: "单", type: "number" },
      { key: "book_team_day_min", label: "团队每日预约时长上限", value: rules.team_day_min || "240", suffix: "分钟", type: "number" },
      { key: "book_horizon_days", label: "团队最远可预约", value: rules.book_horizon_days || "7", suffix: "天", type: "number" },
      { key: "start_early_min", label: "允许提前开始", value: rules.start_early_min || "30", suffix: "分钟", type: "number" },
      { key: "book_grace_min", label: "预约迟到释放宽限", value: rules.grace_min || "15", suffix: "分钟", type: "number" },
      { key: "overrun_remind_min", label: "超时提醒间隔", value: rules.overrun_remind_min || "30", suffix: "分钟", type: "number" },
      { key: "reminder_minutes", label: "日程提前提醒", value: String(reminder.minutes || 15), suffix: "分钟", type: "number" },
      { key: "rule_dev_note", label: "设备组必须填写问题", value: String(rules.dev_note || "1"), type: "switch" },
      { key: "rule_biange_media", label: "编歌必须上传附件", value: String(rules.biange_media || "1"), type: "switch" },
      { key: "rule_done_note", label: "完成任务必须填写说明", value: String(rules.done_note || "1"), type: "switch" },
      { key: "urgent_on", label: "开启加急通知", value: this.data.state.urgent_on ? "1" : "0", type: "switch" },
      { key: "reminder_on", label: "开启日程提醒", value: reminder.on ? "1" : "0", type: "switch" },
    ];
    this.setData({ ruleItems: items });
  },

  openCreate() {
    const tab = this.data.tab;
    if (!["teams", "sla", "shifts", "groups"].includes(tab)) return;
    const defaults = {
      teams: { name: "", room: "", rehearsal: "", onair: "" },
      sla: { task_type: "", grp: (this.data.state.groups[0] || {}).name || "", minutes: 30, buffer: 0, note: "" },
      shifts: { name: "", start: "09:00", end: "18:00", sort: (this.data.state.shifts || []).length + 1 },
      groups: { name: "", color: "#D8922B", sort: (this.data.state.groups || []).length + 1, auto: 0 },
    };
    this.openForm(tab, defaults[tab], true);
  },

  openEdit(event) {
    const id = event.currentTarget.dataset.id;
    const item = this.data.list.find((record) => String(record.open_id || record.id) === String(id));
    if (!item) return;
    if (this.data.tab === "accounts") {
      const type = this.accountType(item);
      const accessKey = item.blocked ? "blocked" : item.approved ? "approved" : "pending";
      const groupIndex = Math.max(0, (this.data.state.groups || []).findIndex((group) => group.name === item.grp));
      const teamIndex = Math.max(0, (this.data.state.teams || []).findIndex((team) => team.id === item.team_id));
      this.setData({
        modal: { entity: "accounts", title: item.name, create: false },
        form: { ...item },
        accountTypeIndex: Math.max(0, ACCOUNT_TYPES.findIndex((entry) => entry.key === type.key)),
        accessIndex: Math.max(0, ACCESS_TYPES.findIndex((entry) => entry.key === accessKey)),
        groupIndex,
        teamIndex,
      });
      return;
    }
    this.openForm(this.data.tab, { ...item }, false);
  },

  openForm(entity, form, create) {
    const titles = { teams: "直播团队", sla: "任务规则", shifts: "班次", groups: "业务分组" };
    const groupIndex = Math.max(0, (this.data.state.groups || []).findIndex((group) => group.name === form.grp));
    this.setData({ modal: { entity, title: titles[entity], create }, form, groupIndex });
  },

  closeModal() {
    if (!this.data.submitting) this.setData({ modal: null, form: {} });
  },

  stopPropagation() {},

  updateField(event) {
    const field = event.currentTarget.dataset.field;
    const form = { ...this.data.form, [field]: event.detail.value };
    this.setData({ form });
  },

  updateSwitchField(event) {
    const field = event.currentTarget.dataset.field;
    this.setData({ form: { ...this.data.form, [field]: event.detail.value ? 1 : 0 } });
  },

  selectFormGroup(event) {
    const groupIndex = Number(event.detail.value) || 0;
    this.setData({ groupIndex, form: { ...this.data.form, grp: this.data.state.groups[groupIndex].name } });
  },

  selectAccountType(event) { this.setData({ accountTypeIndex: Number(event.detail.value) || 0 }); },
  selectAccess(event) { this.setData({ accessIndex: Number(event.detail.value) || 0 }); },
  selectAccountGroup(event) { this.setData({ groupIndex: Number(event.detail.value) || 0 }); },
  selectAccountTeam(event) { this.setData({ teamIndex: Number(event.detail.value) || 0 }); },

  async saveForm() {
    if (this.data.submitting || !this.data.modal) return;
    let entity = this.data.modal.entity;
    let record = { ...this.data.form };
    if (entity === "accounts") {
      entity = "users";
      const type = ACCOUNT_TYPES[this.data.accountTypeIndex].key;
      const access = ACCESS_TYPES[this.data.accessIndex].key;
      record = {
        open_id: this.data.form.open_id,
        alias: String(this.data.form.alias || "").trim(),
        approved: access === "approved" ? "1" : "0",
        blocked: access === "blocked" ? "1" : "0",
        read_only: "0",
        team_id: "",
        grp: "",
        lead: "0",
        intern: "0",
      };
      if (type === "team") {
        const team = this.data.state.teams[this.data.teamIndex];
        Object.assign(record, { role: "team", team_id: team ? team.id : "" });
      } else if (["staff", "intern", "lead"].includes(type)) {
        const group = this.data.state.groups[this.data.groupIndex];
        Object.assign(record, { role: "staff", grp: group ? group.name : "", intern: type === "intern" ? "1" : "0", lead: type === "lead" ? "1" : "0" });
      } else {
        Object.assign(record, { role: "admin", read_only: type === "readonly" ? "1" : "0" });
      }
    }
    this.setData({ submitting: true });
    try {
      await app.post("/api/save", { entity, record });
      wx.showToast({ title: "配置已保存", icon: "success" });
      this.setData({ modal: null, form: {}, submitting: false });
      await this.load(true);
    } catch (error) {
      this.setData({ submitting: false });
      app.showError(error);
    }
  },

  removeItem(event) {
    const entity = this.data.tab;
    const id = Number(event.currentTarget.dataset.id);
    if (!["teams", "sla", "shifts", "groups"].includes(entity) || !id) return;
    wx.showModal({
      title: "确认删除",
      content: "删除前系统会检查是否仍被工单或人员引用。",
      confirmText: "删除",
      confirmColor: "#A63D31",
      success: async (result) => {
        if (!result.confirm) return;
        try {
          await app.post("/api/remove", { entity, id });
          wx.showToast({ title: "已删除", icon: "success" });
          await this.load(true);
        } catch (error) {
          app.showError(error);
        }
      },
    });
  },

  async saveRule(event) {
    const key = event.currentTarget.dataset.key;
    const type = event.currentTarget.dataset.type;
    const value = type === "switch" ? (event.detail.value ? "1" : "0") : String(event.detail.value || "").trim();
    try {
      await app.post("/api/setting", { key, value });
      wx.showToast({ title: "规则已更新", icon: "success" });
      await this.load(true);
    } catch (error) {
      app.showError(error);
    }
  },
});
