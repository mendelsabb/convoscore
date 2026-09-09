import { NavLink, Route, Routes } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";

import { api } from "./api/client";
import { Overview } from "./pages/Overview";
import { Submit } from "./pages/Submit";
import { Conversations } from "./pages/Conversations";
import { ConversationDetailPage } from "./pages/ConversationDetail";

function HealthIndicator() {
  const { data, isError } = useQuery({
    queryKey: ["health"],
    queryFn: api.getHealth,
    refetchInterval: 15000,
  });

  if (isError) {
    return (
      <span className="row faint">
        <span className="dot dot-bad" /> API unreachable
      </span>
    );
  }
  if (!data) return null;

  const unhealthy = data.dependencies.filter((dependency) => !dependency.healthy);
  return (
    <span className="row faint" title={data.dependencies.map((d) => `${d.name}: ${d.detail}`).join("\n")}>
      <span className={`dot ${unhealthy.length === 0 ? "dot-ok" : "dot-bad"}`} />
      {unhealthy.length === 0 ? "All systems healthy" : `${unhealthy.map((d) => d.name).join(", ")} down`}
    </span>
  );
}

export function App() {
  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          Convo<span>Score</span>
        </div>
        <nav className="nav">
          <NavLink to="/" end>
            Overview
          </NavLink>
          <NavLink to="/submit">Submit</NavLink>
          <NavLink to="/conversations">Conversations</NavLink>
        </nav>
        <div className="topbar-right">
          <HealthIndicator />
        </div>
      </header>

      <main>
        <Routes>
          <Route path="/" element={<Overview />} />
          <Route path="/submit" element={<Submit />} />
          <Route path="/conversations" element={<Conversations />} />
          <Route path="/conversations/:id" element={<ConversationDetailPage />} />
          <Route
            path="*"
            element={
              <div className="empty">
                Page not found. <NavLink to="/">Back to the overview</NavLink>
              </div>
            }
          />
        </Routes>
      </main>
    </div>
  );
}
