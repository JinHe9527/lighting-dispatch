const app = getApp();

Page({
  data: {
    loading: true,
    me: null,
    rows: [],
    visibleRows: [],
    search: "",
    errorMessage: "",
  },

  onShow() {
    this.load();
  },

  onPullDownRefresh() {
    this.load().finally(() => wx.stopPullDownRefresh());
  },

  async load() {
    this.setData({ loading: true, errorMessage: "" });
    try {
      const state = await app.loadState(false);
      if (state.me.role !== "admin") {
        wx.navigateBack();
        return;
      }
      const result = await app.get("/api/audit");
      this.setData({ loading: false, me: state.me, rows: result.data || [] }, () => this.applyFilter());
    } catch (error) {
      if (error.statusCode === 401) return app.showError(error);
      this.setData({ loading: false, errorMessage: error.message || "审计记录加载失败" });
    }
  },

  handleSearch(event) {
    this.setData({ search: event.detail.value || "" }, () => this.applyFilter());
  },

  applyFilter() {
    const keyword = String(this.data.search || "").trim().toLowerCase();
    const visibleRows = keyword
      ? this.data.rows.filter((row) => [row.name, row.action, row.detail, row.ts].some((value) => String(value || "").toLowerCase().includes(keyword)))
      : this.data.rows;
    this.setData({ visibleRows });
  },
});
