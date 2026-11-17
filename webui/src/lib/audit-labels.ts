import type { AuditRecord, User } from "../types";

export const personActionLabels: Record<string, string> = {
  "user.login": "登录后台",
  "user.initialize": "初始化工作区",
  "user.logout": "退出登录",
  "user.create": "添加成员",
  "user.update": "修改成员",
  "user.delete": "删除成员",
  "instance.create": "添加数据库实例",
  "instance.update": "更新数据库实例",
  "instance.delete": "删除数据库实例",
  "instance.test": "测试数据库连接",
  "workspace.create": "创建工作空间",
  "workspace.update": "重命名工作空间",
  "workspace.delete": "删除工作空间",
  "token.create": "创建 Token",
  "token.copy": "复制 Token",
  "token.update": "更新 Token",
  "token.delete": "删除 Token",
  "token.revoke": "吊销 Token",
  "audit.export": "导出审计",
  "config.export": "导出实例配置",
};

export function personActionLabel(action?: string | null) {
  return personActionLabels[action || ""] || "其他操作";
}

export function personSummaryLabel(record: AuditRecord) {
  if (!record.summary) return "—";
  if (record.action !== "user.update" && record.action !== "token.update")
    return record.summary;
  const labels: Record<string, string> = {
    display_name: "显示名称",
    enabled: record.object_type === "user" ? "登录状态" : "启用状态",
    name: "Token 名称",
    description: "用途说明",
    密码已更换: "密码",
  };
  const fields = record.summary.split(",").map((field) => field.trim());
  return fields.every((field) => Object.hasOwn(labels, field))
    ? `修改${fields.map((field) => labels[field]).join("、")}`
    : record.summary;
}

export function personObjectLabel(record: AuditRecord, users: User[]) {
  if (record.action === "user.login" || record.action === "user.logout")
    return "管理后台";
  switch (record.object_type) {
    case "user": {
      const user = users.find((item) => item.id === record.object_id);
      const name =
        record.object_id && record.object_id === record.actor_user_id
          ? record.username || user?.username
          : user?.username;
      return name ? `成员 · ${name}` : "成员账号";
    }
    case "workspace":
      return record.workspace_name ? `工作空间 · ${record.workspace_name}` : "工作空间";
    case "token":
      return record.token_name ? `Token · ${record.token_name}` : "MCP Token";
    case "instance":
      return record.object_id ? `数据库实例 · ${record.object_id}` : "数据库实例";
    case "config":
      return "实例配置";
    case "audit":
      return "审计记录";
    case "session":
      return "登录会话";
    default:
      return "其他对象";
  }
}
