const { pageEntries } = require("../../utils/access");

Component({
  properties: {
    active: { type: String, value: "home" },
    user: { type: Object, value: null },
  },

  data: { items: [] },

  observers: {
    user(user) {
      this.setData({ items: pageEntries(user) });
    },
  },

  methods: {
    navigate(event) {
      const item = event.currentTarget.dataset.item;
      if (!item || item.key === this.data.active) return;
      wx.redirectTo({ url: item.path });
    },
  },
});
