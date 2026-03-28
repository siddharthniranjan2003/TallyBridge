import express from "express";
import cors from "cors";
import dotenv from "dotenv";
dotenv.config();

import syncRouter from "./routes/sync.js";

const app = express();
app.use(cors());
app.use(express.json({ limit: "50mb" }));

app.use("/api/sync", syncRouter);

app.get("/health", (_, res) => {
  res.json({ status: "ok", service: "TallyBridge API" });
});

const PORT = process.env.PORT || 3001;
app.listen(PORT, () => {
  console.log(`[TallyBridge API] Running on port ${PORT}`);
});