import { timingSafeEqual } from 'crypto';
import type { Request, Response, NextFunction } from 'express';
import * as Sentry from '@sentry/node';
import { firebaseAuth } from '../db/firebase.js';

export async function requireApiKey(
  req: Request,
  res: Response,
  next: NextFunction,
): Promise<void> {
  // Firebase JWT (Flutter app users)
  const authHeader = req.headers['authorization'];
  if (authHeader?.startsWith('Bearer ')) {
    const token = authHeader.slice(7);
    try {
      const decoded = await firebaseAuth.verifyIdToken(token);
      (req as Request & { firebaseUser: typeof decoded }).firebaseUser = decoded;
      // Attribute any error captured during this request to the calling user.
      Sentry.getIsolationScope().setUser({ id: decoded.uid });
      next();
      return;
    } catch {
      res.status(401).json({ error: 'Invalid or expired token' });
      return;
    }
  }

  // API key fallback (service-to-service)
  const headerValue = req.headers['x-api-key'];
  const key = Array.isArray(headerValue) ? headerValue[0] : headerValue;
  const expectedKey = process.env.API_KEY;

  if (!key || !expectedKey) {
    res.status(401).json({ error: 'Unauthorized' });
    return;
  }

  const provided = Buffer.from(key);
  const expected = Buffer.from(expectedKey);

  if (provided.length !== expected.length || !timingSafeEqual(provided, expected)) {
    res.status(401).json({ error: 'Unauthorized' });
    return;
  }

  next();
}
