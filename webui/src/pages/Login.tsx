import { Database } from "lucide-react";
import { ConsoleLogin, type ConsoleLoginProps } from "../components/ConsoleLogin";

type LoginProps = Pick<ConsoleLoginProps, "setup" | "notice" | "noticeType" | "onLogin">;
export default function Login(props: LoginProps) {
  return <ConsoleLogin {...props}
    brand="DB MCP"
    subtitle="Database Console"
    icon={Database}
    headline={<>让每次数据库操作<br />都有清晰的来路。</>}
    description="连接 PostgreSQL 与 Redis，管理访问凭证，追溯每一次查询和变更。一个工作区，保留完整的数据库操作上下文。"
    clientLabel="MCP 客户端"
    repositoryUrl="https://github.com/Lixiuxiu559/db-mcp"
  />;
}
