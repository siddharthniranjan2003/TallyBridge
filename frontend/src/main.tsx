import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, Routes, Route } from "react-router-dom";
import Layout from "./components/Layout";
import Dashboard from "./pages/Dashboard";
import Outstanding from "./pages/Outstanding";
import Sales from "./pages/Sales";
import Inventory from "./pages/Inventory";
import ProfitLoss from "./pages/ProfitLoss";
import BalanceSheet from "./pages/BalanceSheet";
import Parties from "./pages/Parties";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Layout />}>
          <Route index element={<Dashboard />} />
          <Route path="outstanding" element={<Outstanding />} />
          <Route path="sales" element={<Sales />} />
          <Route path="inventory" element={<Inventory />} />
          <Route path="parties" element={<Parties />} />
          <Route path="pnl" element={<ProfitLoss />} />
          <Route path="balance-sheet" element={<BalanceSheet />} />
        </Route>
      </Routes>
    </BrowserRouter>
  </React.StrictMode>
);