import { initializeApp, getApps, cert } from 'firebase-admin/app';
import { getAuth, type Auth } from 'firebase-admin/auth';
import dotenv from 'dotenv';
dotenv.config();

/**
 * Firebase is initialised LAZILY, on the first request that actually presents a
 * Bearer token — not at module load.
 *
 * This used to be a top-level `getAuth()` guarded by
 *   if (!b64) throw new Error('FIREBASE_SERVICE_ACCOUNT_B64 env var not set');
 * which made the variable mandatory for the process to start at all, because
 * middleware/auth.ts imports this module and index.ts imports that.
 *
 * That is wrong for the on-premise deployment. There, every caller authenticates
 * with `x-api-key` and nothing ever sends a Firebase JWT, so the only effect of
 * the eager check was to force a Google service-account PRIVATE KEY onto the
 * client's PC purely to let the process boot. Deferring it means the on-prem
 * installer ships no Google credential at all.
 *
 * Cloud behaviour is unchanged: the variable is set there, the first Bearer
 * request initialises the SDK, and every later one reuses it. The failure mode
 * moves from "backend will not start" to "Bearer requests get 401", which is
 * both narrower and more accurate — an API-key-only deployment genuinely cannot
 * verify Firebase tokens.
 */
let cachedAuth: Auth | null = null;

export function getFirebaseAuth(): Auth {
  if (cachedAuth) return cachedAuth;

  if (!getApps().length) {
    const b64 = process.env.FIREBASE_SERVICE_ACCOUNT_B64;
    if (!b64) throw new Error('FIREBASE_SERVICE_ACCOUNT_B64 env var not set');
    const serviceAccount = JSON.parse(Buffer.from(b64, 'base64').toString('utf-8'));
    initializeApp({ credential: cert(serviceAccount) });
  }

  cachedAuth = getAuth();
  return cachedAuth;
}
