import { Router } from "express";
import { request as httpRequest } from "node:http";
import { request as httpsRequest } from "node:https";
import { requireApiKey } from "../middleware/auth.js";
const router = Router();
function resolveLocalPushUrl() {
    return (process.env.TALLYBRIDGE_LOCAL_PUSH_URL || "http://127.0.0.1:3002/push-voucher").trim();
}
function resolveLocalHealthUrl() {
    return (process.env.TALLYBRIDGE_LOCAL_HEALTH_URL || "http://127.0.0.1:3002/health").trim();
}
function postJson(urlText, payload) {
    return new Promise((resolve, reject) => {
        const target = new URL(urlText);
        const body = JSON.stringify(payload);
        const requestFn = target.protocol === "https:" ? httpsRequest : httpRequest;
        const req = requestFn({
            protocol: target.protocol,
            hostname: target.hostname,
            port: target.port,
            path: `${target.pathname}${target.search}`,
            method: "POST",
            headers: {
                "content-type": "application/json",
                "content-length": Buffer.byteLength(body).toString(),
            },
        }, (res) => {
            const chunks = [];
            res.on("data", (chunk) => chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk)));
            res.on("end", () => {
                resolve({
                    statusCode: res.statusCode || 502,
                    body: Buffer.concat(chunks).toString("utf8"),
                });
            });
        });
        req.on("error", reject);
        req.write(body);
        req.end();
    });
}
function getJson(urlText) {
    return new Promise((resolve, reject) => {
        const target = new URL(urlText);
        const requestFn = target.protocol === "https:" ? httpsRequest : httpRequest;
        const req = requestFn({
            protocol: target.protocol,
            hostname: target.hostname,
            port: target.port,
            path: `${target.pathname}${target.search}`,
            method: "GET",
        }, (res) => {
            const chunks = [];
            res.on("data", (chunk) => chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk)));
            res.on("end", () => {
                resolve({
                    statusCode: res.statusCode || 502,
                    body: Buffer.concat(chunks).toString("utf8"),
                });
            });
        });
        req.on("error", reject);
        req.end();
    });
}
function parseForwardedBody(body) {
    try {
        return JSON.parse(body);
    }
    catch {
        return {
            ok: false,
            error: "TallyBridge returned a non-JSON response",
            raw: body,
        };
    }
}
router.get("/health", requireApiKey, async (_req, res) => {
    try {
        const forwarded = await getJson(resolveLocalHealthUrl());
        res.status(forwarded.statusCode).json(parseForwardedBody(forwarded.body));
    }
    catch (error) {
        const message = error instanceof Error ? error.message : "Unknown local bridge error";
        res.status(502).json({
            ok: false,
            error: `Could not reach local TallyBridge service: ${message}`,
        });
    }
});
router.post("/", requireApiKey, async (req, res) => {
    const payload = req.body;
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
        return res.status(400).json({
            ok: false,
            error: "Voucher payload must be a JSON object",
        });
    }
    try {
        const forwarded = await postJson(resolveLocalPushUrl(), payload);
        res.status(forwarded.statusCode).json(parseForwardedBody(forwarded.body));
    }
    catch (error) {
        const message = error instanceof Error ? error.message : "Unknown local bridge error";
        res.status(502).json({
            ok: false,
            error: `Could not reach local TallyBridge service: ${message}`,
        });
    }
});
export default router;
//# sourceMappingURL=push-voucher.js.map