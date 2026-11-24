import { registerMessages } from "../index";

registerMessages({
  zh: {
    "cap.loading": "正在聚合业务能力图谱（资源 × 动作 × 角色 × 状态机）...",
    "cap.errorLoad": "加载业务能力图谱失败。",
    "cap.errorPromote": "生成 5 用例业务场景失败。",
    "cap.emptyTitle": "暂无业务能力聚类数据",
    "cap.emptyDesc":
      "请先通过「资产探索」或「测试运行」导入 HAR / cURL 流量，系统将自动按 资源 × 动作 × 角色 × 状态机 聚类压缩接口资产。",
    "cap.emptyFilteredTitle": "没有符合当前筛选条件的业务能力",
    "cap.emptyFilteredDesc": "尝试关闭「仅看活跃能力」/「仅含写接口」开关，或清空搜索与模块筛选。",
    "cap.clearFilters": "重置筛选条件",
    "cap.refresh": "刷新",

    // 5-Dimensional Quality KPI Strip
    "cap.kpi.compressionTitle": "能力压缩与覆盖",
    "cap.kpi.compressionSub": "由 {endpoints} 个接口自动聚类（覆盖率 {pct}%）",
    "cap.kpi.stateTitle": "状态机覆盖",
    "cap.kpi.stateSub": "{pct}% · 已识别 {total} 个含状态流转能力",
    "cap.kpi.roleTitle": "角色权限矩阵",
    "cap.kpi.roleSub": "已验证 {covered} / {total} 个角色权限单元",
    "cap.kpi.negativeTitle": "异常路径覆盖",
    "cap.kpi.negativeSub": "已测 {tested} / {total} 个异常边界槽位",
    "cap.kpi.invariantTitle": "不变量断言通过率",
    "cap.kpi.invariantSub": "已通过 {passed} / {total} 条业务不变量断言",

    // Filter & Module Bar
    "cap.searchPlaceholder": "搜索能力名、资源、动作、角色或接口路径…",
    "cap.filter.liveOnly": "仅看活跃能力",
    "cap.filter.hasWrites": "仅含写接口",
    "cap.filter.roleLabel": "角色筛选",
    "cap.filter.allRoles": "全部角色",
    "cap.filter.allModules": "全部模块",
    "cap.filter.showing": "当前展示 {count} 项能力",

    // Role labels
    "cap.role.admin": "管理端 admin",
    "cap.role.pharmacy": "药房端 pharmacy",
    "cap.role.doctor": "医生端 doctor",
    "cap.role.assistant": "医助端 assistant",
    "cap.role.patient": "患者端 patient",
    "cap.role.common": "通用 common",

    // Capability Card
    "cap.liveRatio": "{live} / {total} 活跃",
    "cap.writesBadge": "{count} 写接口",
    "cap.stateMachineBadge": "状态机",
    "cap.rolesLabel": "涉及角色",
    "cap.actionsLabel": "业务动作",
    "cap.statesLabel": "观测状态值",
    "cap.caseMatrixLabel": "5 用例有效性矩阵",
    "cap.caseCoveragePct": "{pct}%",
    "cap.slot.happy_path": "正向",
    "cap.slot.invalid_params": "缺参",
    "cap.slot.illegal_state": "状态机",
    "cap.slot.authz_role": "越权",
    "cap.slot.consistency_idempotency": "幂等",
    "cap.slotStatus.covered": "已覆盖",
    "cap.slotStatus.failed": "失败",
    "cap.slotStatus.pending": "待验证",
    "cap.slotStatus.missing": "缺失",

    // Promote to 5-Case Scenario
    "cap.promoteBtn": "生成 5 用例场景",
    "cap.promoteUpdateBtn": "同步 5 用例场景",
    "cap.promoting": "生成中…",
    "cap.promotedToast": "已为「{capability}」生成 5 用例业务场景：{scenario}",
    "cap.openScenario": "查看业务场景 →",
    "cap.linkedScenariosLabel": "已关联场景",

    // Expanded Endpoints Table
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
    "cap.loading": "Synthesizing Business Capability Map (Resource × Action × Role × State)...",
    "cap.errorLoad": "Failed to load Business Capability Map.",
    "cap.errorPromote": "Failed to promote capability to 5-case scenario.",
    "cap.emptyTitle": "No Business Capabilities Synthesized Yet",
    "cap.emptyDesc":
      "Import HAR / cURL traffic via Discovery or Runs to cluster flat endpoints into Resource × Action × Role × State business capabilities.",
    "cap.emptyFilteredTitle": "No Capabilities Match Current Filters",
    "cap.emptyFilteredDesc":
      "Try toggling off 'Live Only' or 'Has Writes', or clearing your search and module filters.",
    "cap.clearFilters": "Reset Filters",
    "cap.refresh": "Refresh",

    // 5-Dimensional Quality KPI Strip
    "cap.kpi.compressionTitle": "Capability Coverage",
    "cap.kpi.compressionSub": "Clustered from {endpoints} endpoints ({pct}% live)",
    "cap.kpi.stateTitle": "State Machine Coverage",
    "cap.kpi.stateSub": "{pct}% · {total} stateful capabilities detected",
    "cap.kpi.roleTitle": "Role AuthZ Matrix",
    "cap.kpi.roleSub": "{covered} / {total} role permission cells covered",
    "cap.kpi.negativeTitle": "Negative Path Coverage",
    "cap.kpi.negativeSub": "{tested} / {total} negative rule slots tested",
    "cap.kpi.invariantTitle": "Invariant Pass Rate",
    "cap.kpi.invariantSub": "{passed} / {total} business invariants passed",

    // Filter & Module Bar
    "cap.searchPlaceholder": "Search capability, resource, action, role, or path…",
    "cap.filter.liveOnly": "Live Only",
    "cap.filter.hasWrites": "Has Writes",
    "cap.filter.roleLabel": "Role",
    "cap.filter.allRoles": "All Roles",
    "cap.filter.allModules": "All Modules",
    "cap.filter.showing": "Showing {count} capabilities",

    // Role labels
    "cap.role.admin": "Admin",
    "cap.role.pharmacy": "Pharmacy",
    "cap.role.doctor": "Doctor",
    "cap.role.assistant": "Assistant",
    "cap.role.patient": "Patient",
    "cap.role.common": "Common",

    // Capability Card
    "cap.liveRatio": "{live} / {total} live",
    "cap.writesBadge": "{count} writes",
    "cap.stateMachineBadge": "Stateful",
    "cap.rolesLabel": "Roles",
    "cap.actionsLabel": "Actions",
    "cap.statesLabel": "Observed States",
    "cap.caseMatrixLabel": "5-Case Matrix",
    "cap.caseCoveragePct": "{pct}%",
    "cap.slot.happy_path": "Happy",
    "cap.slot.invalid_params": "Params",
    "cap.slot.illegal_state": "State",
    "cap.slot.authz_role": "AuthZ",
    "cap.slot.consistency_idempotency": "Idemp",
    "cap.slotStatus.covered": "Covered",
    "cap.slotStatus.failed": "Failed",
    "cap.slotStatus.pending": "Pending",
    "cap.slotStatus.missing": "Missing",

    // Promote to 5-Case Scenario
    "cap.promoteBtn": "Promote to 5-Case Scenario",
    "cap.promoteUpdateBtn": "Sync 5-Case Scenario",
    "cap.promoting": "Promoting…",
    "cap.promotedToast": "Promoted '{capability}' into 5-case scenario: {scenario}",
    "cap.openScenario": "Open Scenario →",
    "cap.linkedScenariosLabel": "Linked Scenarios",

    // Expanded Endpoints Table
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
