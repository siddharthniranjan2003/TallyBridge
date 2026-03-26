import { HashRouter, Routes, Route, Navigate } from "react-router-dom";
import Sidebar from "./components/Sidebar";
import StatusBar from "./components/StatusBar";
import Home from "./pages/Home";
import AddCompany from "./pages/AddCompany";
import Settings from "./pages/Settings";
import SyncLog from "./pages/SyncLog";
import About from "./pages/About";

export default function App() {
  return (
    <HashRouter>
      <div style={{ display: "flex", height: "100vh", flexDirection: "column" }}>
        <div style={{ display: "flex", flex: 1, overflow: "hidden" }}>
          <Sidebar />
          <main style={{ flex: 1, overflowY: "auto", background: "#f1f3f5" }}>
            <Routes>
              <Route path="/" element={<Home />} />
              <Route path="/add-company" element={<AddCompany />} />
              <Route path="/settings" element={<Settings />} />
              <Route path="/log" element={<SyncLog />} />
              <Route path="/about" element={<About />} />
              <Route path="*" element={<Navigate to="/" />} />
            </Routes>
          </main>
        </div>
        <StatusBar />
      </div>
    </HashRouter>
  );
}