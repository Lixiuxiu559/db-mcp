import { useState, type FormEvent } from "react";
import { ArrowRight, Database, LockKeyhole } from "lucide-react";
import { api, json, setCsrfToken } from "../api";
import type { Session } from "../types";
import { Button } from "../components/ui/button";
import { Field, Input, PasswordInput } from "../components/ui/field";
import { Feedback } from "../components/ui/feedback";
import { ThemeControl } from "../components/theme-provider";
import { errorText } from "../lib/utils";
export default function Login({
  setup,
  notice,
  onLogin,
}: {
  setup: boolean;
  notice?: string;
  onLogin: (session: Session) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [setupKey, setSetupKey] = useState("");
  async function submit(event: FormEvent) {
    event.preventDefault();
    setError("");
    if (setup && password !== confirm) {
      setError("两次输入的密码不一致");
      return;
    }
    setBusy(true);
    try {
      const result = await api<Session>(
        `/api/auth/${setup ? "setup" : "login"}`,
        json("POST", {
          username,
          password,
          ...(setup ? { setup_key: setupKey } : {}),
        }),
      );
      setCsrfToken(result.csrf_token);
      onLogin(result);
    } catch (error) {
      setError(errorText(error));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="login-page">
      <header className="login-top">
        <a className="wordmark" href="./">
          <Database size={20} aria-hidden="true" />
          <strong>DB MCP Gateway</strong>
          <span>Database Console</span>
        </a>
        <div className="login-preferences"><ThemeControl /></div>
      </header>
      <main className="login-layout">
        <section className="login-card">
          <div className="login-card-heading">
            <span className="login-emblem"><LockKeyhole size={20} aria-hidden="true" /></span>
            <h1>{setup ? "建立第一个用户" : "登录工作区"}</h1>
            <p>
              {setup
                ? "使用服务端初始化密钥创建账号，原有 MCP token 会继续保留。"
                : "使用你的用户名与密码继续。"}
            </p>
          </div>
          <form onSubmit={submit} className="form-stack">
            <Feedback message={error || notice} />
            {setup && (
              <Field
                label="初始化密钥"
                hint="从服务启动日志提示的 setup.key 文件读取。"
              >
                <PasswordInput
                  value={setupKey}
                  onChange={(e) => setSetupKey(e.target.value)}
                  autoComplete="off"
                  required
                  placeholder="服务端一次性初始化密钥"
                />
              </Field>
            )}
            <Field label="用户名">
              <Input
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                autoComplete="username"
                required
                maxLength={64}
                placeholder="输入用户名"
              />
            </Field>
            <Field
              label="密码"
              hint={setup ? "至少 10 位，请使用独立的登录密码。" : undefined}
            >
              <PasswordInput
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete={setup ? "new-password" : "current-password"}
                minLength={setup ? 10 : undefined}
                maxLength={1024}
                required
                placeholder="输入密码"
              />
            </Field>
            {setup && (
              <Field label="确认密码">
                <PasswordInput
                  value={confirm}
                  onChange={(e) => setConfirm(e.target.value)}
                  autoComplete="new-password"
                  required
                  placeholder="再次输入密码"
                />
              </Field>
            )}
            <Button
              variant="primary"
              className="login-submit"
              disabled={busy}
              type="submit"
            >
              {busy ? "正在处理…" : setup ? "创建用户并进入" : "登录"}
              <ArrowRight size={16} />
            </Button>
          </form>
          <p className="login-footnote">
            {setup
              ? "初始化完成后此入口将关闭，所有启用人员具有相同管理能力。"
              : "登录状态仅用于管理后台，不作为 MCP 调用凭证。"}
          </p>
        </section>
      </main>
      <footer className="login-footer">
        DB MCP<span>PostgreSQL / Redis · Database Console</span>
      </footer>
    </div>
  );
}
