import { Outlet, NavLink } from "react-router-dom";
import { COMPANY } from "../api/client";

const links = [
  { to: "/", label: "Dashboard", icon: "⊞" },
  { to: "/outstanding", label: "Outstanding", icon: "💰" },
  { to: "/sales", label: "Sales", icon: "🧾" },
  { to: "/inventory", label: "Inventory", icon: "📦" },
  { to: "/parties", label: "Parties", icon: "👤" },
  { to: "/pnl", label: "P&L", icon: "📈" },
  { to: "/balance-sheet", label: "Balance Sheet", icon: "⚖️" },
];

export default function Layout() {
  return (
    <div style={{ display: "flex", height: "100vh", background: "#f8f9fa" }}>
      {/* Sidebar */}
      <aside style={{
        width: 220, background: "#1a1a2e", color: "#fff",
        display: "flex", flexDirection: "column", flexShrink: 0,
      }}>
        <div style={{ padding: "20px 16px", borderBottom: "1px solid rgba(255,255,255,0.08)" }}>
          <div style={{ fontSize: 17, fontWeight: 600 }}>TallyBridge</div>
          <div style={{
            fontSize: 11, color: "rgba(255,255,255,0.35)", marginTop: 3,
            whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis"
          }}>
            {COMPANY}
          </div>
        </div>

        <nav style={{ flex: 1, padding: "10px 8px" }}>
          {links.map(l => (
            <NavLink
              key={l.to}
              to={l.to}
              end={l.to === "/"}
              style={({ isActive }) => ({
                display: "flex", alignItems: "center", gap: 10,
                padding: "9px 12px", borderRadius: 8, marginBottom: 2,
                textDecoration: "none", fontSize: 13, transition: "all 0.15s",
                color: isActive ? "#fff" : "rgba(255,255,255,0.5)",
                background: isActive ? "rgba(255,255,255,0.1)" : "transparent",
              })}
            >
              <span style={{ fontSize: 15 }}>{l.icon}</span>
              {l.label}
            </NavLink>
          ))}
        </nav>

        <div style={{
          padding: "12px 16px", fontSize: 11,
          color: "rgba(255,255,255,0.2)", borderTop: "1px solid rgba(255,255,255,0.08)"
        }}>
          TallyBridge Web v1.0
        </div>
      </aside>

      {/* Content */}
      <main style={{ flex: 1, overflowY: "auto" }}>
        <Outlet />
      </main>
    </div>
  );
}