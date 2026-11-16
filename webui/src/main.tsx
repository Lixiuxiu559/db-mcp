import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "antd/dist/reset.css";
import "./global.css";
import App from "./App";
import { ConsoleProvider } from "./components/ConsoleProvider";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ConsoleProvider themeKey="db-mcp-theme" cssVarKey="db-mcp">
      <App />
    </ConsoleProvider>
  </StrictMode>,
);
