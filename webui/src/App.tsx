import { useEffect, useState } from "react";
import {
  Database, LayoutDashboard, KeyRound, Users, Activity, History,
  Search, LogOut, UserRound, CircleCheck, CircleAlert, PanelLeftClose, PanelLeftOpen,
} from "lucide-react";
import { api, ApiError, json, setCsrfToken } from "./api";
import type { Page, Session } from "./types";
import Login from "./pages/Login";
import Overview from "./pages/Overview";
import Instances from "./pages/Instances";
import UsersPage from "./pages/Users";
import Tokens from "./pages/Tokens";
import Audit from "./pages/Audit";
import { Button } from "./components/ui/button";
import { Feedback } from "./components/ui/feedback";
import { Modal } from "./components/ui/dialog";
import { ThemeControl, ThemeToggle } from "./components/theme-provider";
import { CommandPalette, navigation } from "./components/CommandPalette";
import { useResource } from "./lib/use-resource";
import { errorText } from "./lib/utils";

const icons = { overview: LayoutDashboard, instances: Database, users: Users, tokens: KeyRound, "mcp-audit": Activity, "person-audit": History };
export default function App() {
  const [session, setSession] = useState<Session | null>(null);
  const [setup, setSetup] = useState(false);
  const [booting, setBooting] = useState(true);
  const [error, setError] = useState("");
  const [expired, setExpired] = useState(false);
  const [page, setPage] = useState<Page>("overview");
  const [command, setCommand] = useState(false);
  const [accountOpen, setAccountOpen] = useState(false);
  const [navOpen, setNavOpen] = useState(false);
  const health = useResource<{ ok: boolean }>("/healthz", { ok: false });
  const healthy = health.data.ok && !health.error;
  const title = navigation.find((item) => item.id === page)?.label || "工作区";

  function closeNav() {
    setNavOpen(false);
    requestAnimationFrame(() => document.getElementById("nav-toggle")?.focus());
  }
  async function boot() {
    setBooting(true);
    setError("");
    try {
      const status = await api<{ setup_required: boolean }>("/api/auth/status");
      setSetup(status.setup_required);
      if (!status.setup_required) {
        try {
          const value = await api<Session>("/api/me");
          setSession(value);
          setCsrfToken(value.csrf_token);
        } catch (error) {
          if (!(error instanceof ApiError && error.status === 401)) throw error;
          setExpired(false);
        }
      }
    } catch (error) {
      setError(errorText(error));
    } finally {
      setBooting(false);
    }
  }
  useEffect(() => { void boot(); }, []);
  useEffect(() => {
    document.title = session ? `${title} · DB MCP Console` : "登录 · DB MCP Console";
  }, [title, session]);
  useEffect(() => {
    const expire = () => {
      setSession(null); setExpired(true); setCsrfToken("");
      setCommand(false); setAccountOpen(false); setNavOpen(false);
    };
    window.addEventListener("session-expired", expire);
    return () => window.removeEventListener("session-expired", expire);
  }, []);
  useEffect(() => {
    if (!navOpen) return;
    const handle = (event: KeyboardEvent) => { if (event.key === "Escape") closeNav(); };
    document.addEventListener("keydown", handle);
    return () => document.removeEventListener("keydown", handle);
  }, [navOpen]);
  useEffect(() => {
    if (!session) return;
    const handle = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setAccountOpen(false);
        setCommand((value) => !value);
      }
    };
    window.addEventListener("keydown", handle);
    return () => window.removeEventListener("keydown", handle);
  }, [session]);

  if (booting) return <div className="boot-state" role="status"><Database size={24} aria-hidden="true" /><p>正在连接工作区…</p></div>;
  if (error && !session) return <div className="boot-state"><Feedback message={error} /><Button onClick={() => void boot()}>重新连接</Button></div>;
  if (!session) return <Login setup={setup} notice={expired ? "登录已过期，请重新登录。" : ""} onLogin={(value) => {
    setSession(value); setSetup(false); setExpired(false); setPage("overview");
  }} />;

  async function logout() {
    try {
      await api("/api/auth/logout", json("POST"));
      setSession(null); setExpired(false); setCsrfToken("");
      setAccountOpen(false); setCommand(false); setNavOpen(false);
    } catch (error) { setError(errorText(error)); }
  }
  function navigate(value: Page) {
    setPage(value); setNavOpen(false); setError("");
    requestAnimationFrame(() => document.getElementById("workspace-content")?.focus());
  }
  return (
    <div className={`workspace ${navOpen ? "nav-expanded" : ""}`}>
      <a className="skip-link" href="#workspace-content">跳到工作区</a>
      <header className="console-header">
        <div className="console-header-start">
          <Button variant="quiet" className="icon-button mobile-nav" id="nav-toggle" aria-label={navOpen ? "关闭导航" : "打开导航"} aria-expanded={navOpen} aria-controls="workspace-nav" onClick={() => navOpen ? closeNav() : setNavOpen(true)}>
            {navOpen ? <PanelLeftClose size={16} /> : <PanelLeftOpen size={16} />}
          </Button>
          <a className="console-brand" href="./">
            <span className="console-brand-icon"><Database size={16} aria-hidden="true" /></span>
            <span><strong>DB MCP Gateway</strong><small>Database Console</small></span>
          </a>
        </div>
        <div className="console-header-actions">
          <Button variant="quiet" className="icon-button command-trigger" aria-label="搜索页面（⌘ K）" onClick={() => setCommand(true)}><Search size={14} /></Button>
          <ThemeToggle />
          <button className="account-trigger" onClick={() => setAccountOpen(true)} aria-label="账号与偏好设置"><UserRound size={14} /></button>
        </div>
      </header>
      <div className="console-body">
        <aside className="sidebar" id="workspace-nav" aria-label="工作区导航">
          <div className="sidebar-caption">工作空间</div>
          <nav aria-label="主导航">{navigation.map((item) => {
            const Icon = icons[item.id];
            return <button key={item.id} type="button" className={`nav-item ${page === item.id ? "current" : ""}`} aria-current={page === item.id ? "page" : undefined} onClick={() => navigate(item.id)}><Icon size={14} aria-hidden="true" /><span>{item.label}</span></button>;
          })}</nav>
          <div className="sidebar-bottom">
            {healthy ? <CircleCheck size={14} className="success-text" aria-hidden="true" /> : <CircleAlert size={14} aria-hidden="true" />}
            <div><strong>{healthy ? "网关运行正常" : "网关状态未知"}</strong><small>DB MCP · v2.1.0</small></div>
          </div>
        </aside>
        {navOpen && <button className="nav-overlay" onClick={closeNav} aria-label="关闭导航" />}
        <div className="workspace-main">
          <main className="page-content" id="workspace-content" tabIndex={-1}>
            <Feedback message={error} onDismiss={() => setError("")} />
            {page === "overview" ? <Overview onNavigate={navigate} healthy={healthy} onRefreshHealth={health.reload} /> : page === "instances" ? <Instances /> : page === "users" ? <UsersPage currentUser={session.user} /> : page === "tokens" ? <Tokens /> : <Audit key={page} kind={page === "mcp-audit" ? "mcp" : "person"} />}
          </main>
        </div>
      </div>
      <Modal open={accountOpen} onOpenChange={setAccountOpen} title="账号与偏好设置" description={session.user.display_name || session.user.username}>
        <div className="form-stack">
          <div className="field"><span className="field-label">界面主题</span><ThemeControl /></div>
          <Button onClick={() => { setAccountOpen(false); setCommand(true); }}><Search size={14} />快速前往 <kbd>⌘ K</kbd></Button>
          <Feedback message={error} onDismiss={() => setError("")} />
          <Button variant="danger" onClick={() => void logout()}><LogOut size={14} />退出登录</Button>
        </div>
      </Modal>
      <CommandPalette open={command} onOpenChange={setCommand} onNavigate={navigate} />
    </div>
  );
}
