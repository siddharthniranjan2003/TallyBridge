import { NavLink } from "react-router-dom";

const links = [
  { to: "/", icon: "⊞", label: "Home" },
  { to: "/log", icon: "≡", label: "Sync Log" },
  { to: "/settings", icon: "⚙", label: "Settings" },
  { to: "/about", icon: "ℹ", label: "About" },
];

export default function Sidebar() {
  return (
    <aside style={{
      width: 200,
      background: "#1a1a2e",
      color: "#fff",
      display: "flex",
      flexDirection: "column",
      flexShrink: 0,
    }}>
      {/* Logo */}
      <div style={{
        padding: "20px 16px 16px",
        borderBottom: "1px solid rgba(255,255,255,0.08)",
      }}>
        <div style={{ fontSize: 18, fontWeight: 600, letterSpacing: "-0.3px" }}>
          TallyBridge
        </div>
        <div style={{ fontSize: 11, color: "rgba(255,255,255,0.4)", marginTop: 2 }}>
          Sync connector
        </div>
      </div>

      {/* Nav links */}
      <nav style={{ flex: 1, padding: "10px 8px" }}>
        {links.map((link) => (
          <NavLink
            key={link.to}
            to={link.to}
            end={link.to === "/"}
            style={({ isActive }) => ({
              display: "flex",
              alignItems: "center",
              gap: 10,
              padding: "9px 10px",
              borderRadius: 8,
              textDecoration: "none",
              color: isActive ? "#fff" : "rgba(255,255,255,0.55)",
              background: isActive ? "rgba(255,255,255,0.1)" : "transparent",
              marginBottom: 2,
              fontSize: 13,
              transition: "all 0.15s",
            })}
          >
            <span style={{ fontSize: 15 }}>{link.icon}</span>
            {link.label}
          </NavLink>
        ))}
      </nav>

      {/* Version */}
      <div style={{
        padding: "12px 16px",
        fontSize: 11,
        color: "rgba(255,255,255,0.25)",
        borderTop: "1px solid rgba(255,255,255,0.08)",
      }}>
        v1.0.0
      </div>
    </aside>
  );
}