import { timingSafeEqual } from "crypto";
export function requireApiKey(req, res, next) {
    const headerValue = req.headers["x-api-key"];
    const key = Array.isArray(headerValue) ? headerValue[0] : headerValue;
    const expectedKey = process.env.API_KEY;
    if (!key || !expectedKey) {
        return res.status(401).json({ error: "Unauthorized" });
    }
    const provided = Buffer.from(key);
    const expected = Buffer.from(expectedKey);
    if (provided.length !== expected.length || !timingSafeEqual(provided, expected)) {
        return res.status(401).json({ error: "Unauthorized" });
    }
    next();
}
//# sourceMappingURL=auth.js.map