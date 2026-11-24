import { registerMessages } from "../index";

registerMessages({
  zh: {
    "cap.loading": "正在聚合业务能力质量矩阵…",
    "cap.errorLoad": "加载业务能力矩阵失败。",
    "cap.errorPromote": "生成 5 用例业务场景失败。",
    "cap.emptyTitle": "暂无业务能力聚类数据",
    "cap.emptyDesc":
      "请先通过「资产探索」或「测试运行」导入 HAR / cURL 流量，系统将自动把零散接口聚合成核心业务能力并追踪 5 道质量防线。",
    "cap.emptyFilteredTitle": "当前筛选条件下没有匹配的业务能力",
    "cap.emptyFilteredDesc": "尝试切换至「全部活跃」或「全部」范围，或清空搜索与模块筛选。",
    "cap.clearFilters": "查看全部能力",
    "cap.refresh": "刷新",

    // Purpose + Legend Header
    "cap.purposeLine":
      "将 {endpoints} 个接口聚合成 {core} 个核心业务能力，按 5 道质量防线一眼定位「哪里没测、哪里测出了缺陷」。",
    "cap.legend.passed": "验证通过",
    "cap.legend.failed": "发现缺陷",
    "cap.legend.pending": "已建待测",
    "cap.legend.missing": "未覆盖",

    // Clean 3-Card Summary Strip
    "cap.summary.coreTitle": "核心业务能力",
    "cap.summary.coreUnit": "个",
    "cap.summary.coreSub": "由 {endpoints} 个接口聚合 · 含 {stateful} 个状态机",
    "cap.summary.matrixTitle": "5 道防线实测态势",
    "cap.summary.matrixPassed": "{count} 通过",
    "cap.summary.matrixFailed": "{count} 缺陷",
    "cap.summary.matrixPending": "{count} 待测",
    "cap.summary.matrixSub": "覆盖正向、缺参、状态机、越权、幂等 5 类用例槽位",
    "cap.summary.invariantTitle": "不变量断言通过率",
    "cap.summary.invariantSub": "已通过 {passed} / {total} 条业务不变量断言",

    // Filter Bar & Scope Switcher
    "cap.searchPlaceholder": "搜索中文能力名、模块、路径或动作…",
    "cap.scope.core": "核心能力",
    "cap.scope.live": "全部活跃",
    "cap.scope.all": "全部",
    "cap.filter.roleLabel": "角色筛选",
    "cap.filter.allRoles": "全部角色",
    "cap.filter.allModules": "全部模块",
    "cap.filter.showing": "共 {count} 项能力",

    // Localized Role Names
    "cap.role.admin": "管理员",
    "cap.role.pharmacy": "药店",
    "cap.role.doctor": "医生",
    "cap.role.assistant": "医助",
    "cap.role.patient": "患者",
    "cap.role.common": "通用",

    // Matrix Table Columns
    "cap.table.colCapability": "业务能力",
    "cap.table.colRoles": "涉及角色",
    "cap.table.colAction": "操作",
    "cap.slot.happy_path": "正向",
    "cap.slot.invalid_params": "缺参",
    "cap.slot.illegal_state": "状态机",
    "cap.slot.authz_role": "越权",
    "cap.slot.consistency_idempotency": "幂等",

    // Matrix Cell Labels & Tooltips
    "cap.cell.failed": "缺陷",
    "cap.cell.pending": "待测",
    "cap.slotStatus.covered": "验证通过",
    "cap.slotStatus.failed": "发现缺陷",
    "cap.slotStatus.pending": "已建待测",
    "cap.slotStatus.missing": "未覆盖",

    // Capability Row Subline & Badges
    "cap.rowSubline": "{live}/{total} 接口 · {writes} 写",
    "cap.rowStatePill": "状态: {vals}",

    // Row Actions & Promote Toast
    "cap.promoteBtn": "生成 5 用例",
    "cap.syncBtnTitle": "同步更新 5 用例场景",
    "cap.viewScenarioBtn": "查看场景",
    "cap.promoting": "生成中…",
    "cap.promotedToast": "已为「{capability}」生成 5 用例场景：{scenario}",
    "cap.openScenario": "打开场景 →",

    // Expanded Row Detail
    "cap.actionsLabel": "业务动作",
    "cap.statesLabel": "观测状态机",
    "cap.linkedScenariosLabel": "已关联场景",
    "cap.endpointsHeader": "聚合接口明细 ({count})",
    "cap.col.method": "方法",
    "cap.col.path": "接口路径",
    "cap.col.source": "来源",
    "cap.col.role": "角色",
    "cap.col.action": "动作",
    "cap.col.replay": "调试",
    "cap.tryIt": "Try It",
    "cap.noEndpoints": "该能力下暂无接口明细。",
  },
  en: {
    "cap.loading": "Synthesizing Business Capability Quality Matrix…",
    "cap.errorLoad": "Failed to load Business Capability Matrix.",
    "cap.errorPromote": "Failed to generate 5-case scenario.",
    "cap.emptyTitle": "No Business Capabilities Synthesized Yet",
    "cap.emptyDesc":
      "Import HAR / cURL traffic via Discovery or Runs to cluster flat endpoints into core business capabilities across 5 QA defense lines.",
    "cap.emptyFilteredTitle": "No Capabilities Match Current Filters",
    "cap.emptyFilteredDesc":
      "Switch scope to 'All Live' or 'All', or clear your search and module filters.",
    "cap.clearFilters": "Show All Capabilities",
    "cap.refresh": "Refresh",

    // Purpose + Legend Header
    "cap.purposeLine":
      "Clusters {endpoints} endpoints into {core} core business capabilities so you can spot coverage gaps and defects across 5 QA defenses in seconds.",
    "cap.legend.passed": "Passed",
    "cap.legend.failed": "Defect Found",
    "cap.legend.pending": "Pending Verification",
    "cap.legend.missing": "Uncovered",

    // Clean 3-Card Summary Strip
    "cap.summary.coreTitle": "Core Capabilities",
    "cap.summary.coreUnit": "",
    "cap.summary.coreSub": "Clustered from {endpoints} endpoints · {stateful} stateful",
    "cap.summary.matrixTitle": "5-Defense Verification Pulse",
    "cap.summary.matrixPassed": "{count} Passed",
    "cap.summary.matrixFailed": "{count} Failed",
    "cap.summary.matrixPending": "{count} Todo",
    "cap.summary.matrixSub": "Happy Path, Params, State Machine, AuthZ & Idempotency slots",
    "cap.summary.invariantTitle": "Invariant Pass Rate",
    "cap.summary.invariantSub": "{passed} / {total} business invariants passed",

    // Filter Bar & Scope Switcher
    "cap.searchPlaceholder": "Search capability name, module, path, or action…",
    "cap.scope.core": "Core Only",
    "cap.scope.live": "All Live",
    "cap.scope.all": "All",
    "cap.filter.roleLabel": "Role",
    "cap.filter.allRoles": "All Roles",
    "cap.filter.allModules": "All Modules",
    "cap.filter.showing": "{count} capabilities",

    // Localized Role Names
    "cap.role.admin": "Admin",
    "cap.role.pharmacy": "Pharmacy",
    "cap.role.doctor": "Doctor",
    "cap.role.assistant": "Assistant",
    "cap.role.patient": "Patient",
    "cap.role.common": "Common",

    // Matrix Table Columns
    "cap.table.colCapability": "Capability",
    "cap.table.colRoles": "Roles",
    "cap.table.colAction": "Action",
    "cap.slot.happy_path": "Happy",
    "cap.slot.invalid_params": "Params",
    "cap.slot.illegal_state": "State",
    "cap.slot.authz_role": "AuthZ",
    "cap.slot.consistency_idempotency": "Idemp",

    // Matrix Cell Labels & Tooltips
    "cap.cell.failed": "Bug",
    "cap.cell.pending": "Todo",
    "cap.slotStatus.covered": "Passed",
    "cap.slotStatus.failed": "Defect Found",
    "cap.slotStatus.pending": "Pending",
    "cap.slotStatus.missing": "Uncovered",

    // Capability Row Subline & Badges
    "cap.rowSubline": "{live}/{total} APIs · {writes} writes",
    "cap.rowStatePill": "State: {vals}",

    // Row Actions & Promote Toast
    "cap.promoteBtn": "Generate 5 Cases",
    "cap.syncBtnTitle": "Sync 5-Case Scenario",
    "cap.viewScenarioBtn": "View Scenario",
    "cap.promoting": "Generating…",
    "cap.promotedToast": "Generated 5-case scenario for '{capability}': {scenario}",
    "cap.openScenario": "Open Scenario →",

    // Expanded Row Detail
    "cap.actionsLabel": "Actions",
    "cap.statesLabel": "Observed States",
    "cap.linkedScenariosLabel": "Linked Scenarios",
    "cap.endpointsHeader": "Grouped Endpoints ({count})",
    "cap.col.method": "Method",
    "cap.col.path": "Endpoint Path",
    "cap.col.source": "Source",
    "cap.col.role": "Role",
    "cap.col.action": "Action",
    "cap.col.replay": "Replay",
    "cap.tryIt": "Try It",
    "cap.noEndpoints": "No endpoints recorded for this capability.",
  },
});
