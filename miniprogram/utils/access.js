const ROLE_LABELS = {
  admin: "管理员",
  staff: "工作人员",
  team: "团队账号",
};

const STATUS_LABELS = {
  pending: "待派工",
  assigned: "已指派",
  in_progress: "进行中",
  completion_review: "待核实",
  completed: "已完成",
  cancelled: "已取消",
  abnormal: "异常",
};

function roleLabel(user) {
  if (user && user.role === "staff" && user.lead) return "组长";
  return ROLE_LABELS[(user && user.role) || ""] || "成员";
}

function statusLabel(status) {
  return STATUS_LABELS[status] || status || "未知";
}

function pageEntries(user) {
  if (!user) return [];
  const common = [
    { key: "home", label: "总览", path: "/pages/home/index" },
    { key: "orders", label: user.role === "team" ? "我的工单" : "工单", path: "/pages/orders/index" },
    { key: "request", label: "下单", path: "/pages/request/index" },
  ];
  if (user.role !== "team") {
    common.push({ key: "schedule", label: "排班", path: "/pages/schedule/index" });
  }
  common.push({ key: "account", label: "我的", path: "/pages/account/index" });
  return common;
}

function managementEntries(user) {
  if (!user) return [];
  const entries = [];
  if (user.role !== "team") {
    entries.push({ key: "people", label: "人员状态", note: "在岗、忙碌与请假", path: "/pages/people/index" });
  }
  entries.push({ key: "stats", label: "业务统计", note: "工单量与实际工时", path: "/pages/stats/index" });
  if (user.role === "admin") {
    entries.push(
      { key: "settings", label: "系统设置", note: "团队、任务、班次与规则", path: "/pages/settings/index" },
      { key: "audit", label: "审计记录", note: "查看关键操作轨迹", path: "/pages/audit/index" },
    );
  }
  return entries;
}

module.exports = {
  roleLabel,
  statusLabel,
  pageEntries,
  managementEntries,
};
