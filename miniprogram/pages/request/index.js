const app = getApp();
const { dateString, timeString } = require("../../utils/time");

function futureTime(minutes) {
  return timeString(new Date(Date.now() + minutes * 60000));
}

Page({
  data: {
    loading: true,
    state: null,
    me: null,
    teams: [],
    groups: [],
    tasks: [],
    staff: [],
    teamIndex: 0,
    groupIndex: 0,
    taskIndex: 0,
    assigneeIndex: 0,
    assignees: [{ open_id: "", name: "自动/暂不指派" }],
    customTask: "",
    useCustomTask: false,
    when: "now",
    date: dateString(),
    at: futureTime(30),
    minutes: 30,
    note: "",
    urgent: false,
    audioFile: null,
    videoFile: null,
    submitting: false,
  },

  onShow() {
    if (!this.data.state) this.load();
  },

  async load() {
    try {
      const state = await app.loadState(true);
      const groups = state.groups || [];
      const groupIndex = 0;
      const tasks = this.tasksForGroup(state, groups[groupIndex] && groups[groupIndex].name);
      this.setData({
        loading: false,
        state,
        me: state.me,
        teams: state.teams || [],
        groups,
        tasks,
        groupIndex,
        taskIndex: 0,
        minutes: Number((tasks[0] && tasks[0].minutes) || 30),
      }, () => this.refreshAssignees());
    } catch (error) {
      app.showError(error);
    }
  },

  tasksForGroup(state, groupName) {
    return (state.sla || []).filter((task) => task.grp === groupName);
  },

  changeTeam(event) {
    this.setData({ teamIndex: Number(event.detail.value) || 0 });
  },

  changeGroup(event) {
    const groupIndex = Number(event.detail.value) || 0;
    const group = this.data.groups[groupIndex];
    const tasks = this.tasksForGroup(this.data.state, group && group.name);
    this.setData({
      groupIndex,
      tasks,
      taskIndex: 0,
      minutes: Number((tasks[0] && tasks[0].minutes) || 30),
      useCustomTask: !tasks.length,
      customTask: "",
    }, () => this.refreshAssignees());
  },

  changeTask(event) {
    const value = Number(event.detail.value) || 0;
    if (value >= this.data.tasks.length) {
      this.setData({ useCustomTask: true, taskIndex: this.data.tasks.length, customTask: "" });
      return;
    }
    const task = this.data.tasks[value];
    this.setData({ taskIndex: value, useCustomTask: false, minutes: Number(task.minutes) || 30 });
  },

  toggleCustomTask() {
    this.setData({ useCustomTask: !this.data.useCustomTask, customTask: "" });
  },

  refreshAssignees() {
    const group = this.data.groups[this.data.groupIndex];
    const me = this.data.me || {};
    const canChoose = me.role === "admin" || (me.role === "staff" && me.lead);
    const candidates = canChoose
      ? (this.data.state.staff || []).filter((person) => person.grp === (group && group.name) && !person.intern)
      : [];
    this.setData({
      staff: candidates,
      assignees: [{ open_id: "", name: "自动/暂不指派" }, ...candidates],
      assigneeIndex: 0,
    });
  },

  changeAssignee(event) {
    this.setData({ assigneeIndex: Number(event.detail.value) || 0 });
  },

  selectWhen(event) {
    this.setData({ when: event.currentTarget.dataset.when });
  },

  changeDate(event) { this.setData({ date: event.detail.value }); },
  changeTime(event) { this.setData({ at: event.detail.value }); },
  changeMinutes(event) { this.setData({ minutes: Math.max(10, Math.min(720, Number(event.detail.value) || 30)) }); },
  changeNote(event) { this.setData({ note: event.detail.value || "" }); },
  changeCustomTask(event) { this.setData({ customTask: event.detail.value || "" }); },
  changeUrgent(event) { this.setData({ urgent: Boolean(event.detail.value) }); },

  chooseAudio() {
    wx.chooseMessageFile({
      count: 1,
      type: "file",
      extension: ["mp3", "m4a", "aac", "wav", "mp4"],
      success: (result) => {
        const file = result.tempFiles && result.tempFiles[0];
        if (file) this.setData({ audioFile: { path: file.path, name: file.name || "音频文件", size: file.size || 0 } });
      },
    });
  },

  chooseVideo() {
    wx.chooseMedia({
      count: 1,
      mediaType: ["video"],
      sourceType: ["album", "camera"],
      success: (result) => {
        const file = result.tempFiles && result.tempFiles[0];
        if (file) this.setData({ videoFile: { path: file.tempFilePath, name: "现场视频", size: file.size || 0 } });
      },
    });
  },

  clearFile(event) {
    const kind = event.currentTarget.dataset.kind;
    this.setData(kind === "audio" ? { audioFile: null } : { videoFile: null });
  },

  async submit() {
    if (this.data.submitting) return;
    const team = this.data.teams[this.data.teamIndex];
    const group = this.data.groups[this.data.groupIndex];
    const task = this.data.tasks[this.data.taskIndex];
    const taskName = this.data.useCustomTask ? this.data.customTask.trim() : (task && task.task_type);
    if (!team) return wx.showToast({ title: "请选择直播团队", icon: "none" });
    if (!group) return wx.showToast({ title: "请选择处理组", icon: "none" });
    if (!taskName) return wx.showToast({ title: "请填写任务内容", icon: "none" });
    this.setData({ submitting: true });
    wx.showLoading({ title: this.data.audioFile || this.data.videoFile ? "上传并下单" : "正在下单", mask: true });
    try {
      const uploadTokens = [];
      if (this.data.audioFile) uploadTokens.push((await app.uploadFile(this.data.audioFile.path)).token);
      if (this.data.videoFile) uploadTokens.push((await app.uploadFile(this.data.videoFile.path)).token);
      const assignee = this.data.assignees[this.data.assigneeIndex];
      await app.post("/api/order", {
        team_id: team.id,
        team: team.name,
        group: group.name,
        task: taskName,
        custom_task: this.data.useCustomTask ? "1" : "0",
        when: this.data.when,
        date: this.data.date,
        at: this.data.at,
        minutes: Number(this.data.minutes) || 30,
        note: this.data.note.trim(),
        urgent: this.data.urgent ? "1" : "0",
        assignee: assignee && assignee.open_id,
        upload_tokens: uploadTokens,
      });
      app.globalData.state = null;
      wx.hideLoading();
      wx.showToast({ title: "工单已创建", icon: "success" });
      setTimeout(() => wx.redirectTo({ url: "/pages/orders/index" }), 500);
    } catch (error) {
      wx.hideLoading();
      app.showError(error);
    } finally {
      this.setData({ submitting: false });
    }
  },
});
