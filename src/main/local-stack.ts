import { spawn, spawnSync, type ChildProcess } from "child_process";
import { createConnection } from "net";
import fs from "fs";
import path from "path";
import { app } from "electron";
import isDev from "electron-is-dev";
import { logger } from "./logger";

/**
 * Supervises the on-machine data stack: PostgreSQL, PostgREST, Caddy and the
 * Express backend, all as child processes of this app.
 *
 * ── Why child processes and not Windows services ────────────────────────────
 *
 * The native stack originally registered three Windows services so the reports
 * survived a login-less reboot. That reason disappears once TallyBridge itself
 * owns the API: if the app is not running, nothing is serving requests anyway,
 * so a database that is up without it serves no one. Child processes also drop
 * the administrator requirement, which was the one step in the old installer
 * that had never actually been run.
 *
 * ── Start order is not cosmetic ─────────────────────────────────────────────
 *
 * PostgREST exits if it cannot reach the database at startup, and the backend's
 * supabase-js client resolves nothing without the gateway. So: postgres ->
 * postgrest -> caddy -> backend, each gated on a real readiness probe rather
 * than a sleep.
 *
 * ── postgres.exe directly, not `pg_ctl start` ───────────────────────────────
 *
 * pg_ctl forks the server and exits, which would leave us supervising a process
 * that is already gone while the database runs unmanaged. Spawning postgres.exe
 * gives a real child handle. Shutdown still goes through `pg_ctl -m fast stop`,
 * because killing the postmaster outright skips the checkpoint and forces
 * recovery on next boot.
 *
 * ── postgrest.exe is not self-contained ─────────────────────────────────────
 *
 * It needs libpq.dll from pgsql\bin. Without it the process exits 0xC0000135
 * writing nothing to any log, which reads exactly like a corrupt binary. Child
 * processes inherit no PATH from a packaged app, so we set it explicitly below.
 */

interface StackEnv {
  [key: string]: string;
}

interface ManagedProcess {
  name: string;
  child: ChildProcess;
  /** Cleared on deliberate shutdown so the exit handler doesn't try to restart. */
  supervised: boolean;
}

const READY_TIMEOUT_MS = 60_000;
const READY_POLL_MS = 400;
/** Crash restarts are capped: a binary that cannot start should surface, not spin. */
const MAX_RESTARTS = 3;

export class LocalStack {
  private processes: ManagedProcess[] = [];
  private restartCounts = new Map<string, number>();
  private env: StackEnv = {};
  private stackDir = "";
  private started = false;
  private backendUrl = "";
  private backendApiKey = "";

  /**
   * Where the backend ended up and the key it will accept. Read after start().
   *
   * These are generated per machine by bootstrap.mjs, so nothing may hardcode
   * them or keep a second copy that can drift -- see the reconciliation in
   * index.ts for why that matters.
   */
  get controlPlane(): { url: string; apiKey: string } {
    return { url: this.backendUrl, apiKey: this.backendApiKey };
  }

  /**
   * Repo layout in dev, `resources\stack` in a packaged build. Everything the
   * stack needs (vendor binaries, config templates, scripts, the built backend)
   * lives under this one directory so packaging is a single extraResources entry.
   */
  private resolveStackDir(): string {
    return isDev
      ? path.join(app.getAppPath(), "native-stack")
      : path.join(process.resourcesPath, "stack");
  }

  /**
   * Where config/.env actually lives between runs.
   *
   * bootstrap.mjs, db-init.ps1, apply-schema.mjs and start.ps1 all read
   * `<stack>\config\.env` and that path is baked into them. But in a packaged
   * build `<stack>` is `resources\stack`, which an app update replaces wholesale
   * -- taking this machine's Postgres password and PGDATA pointer with it and
   * leaving the data directory unreadable. Losing the books to an auto-update is
   * not an acceptable failure mode.
   *
   * So ProgramData holds the authoritative copy and we mirror it into the stack
   * directory before anything reads it, and back again after bootstrap writes a
   * fresh one. That keeps every existing script working unmodified -- they still
   * only know about their own config\ -- which matters because those scripts are
   * also the by-hand repair path.
   *
   * In dev the two are the same directory and the mirroring is a no-op.
   */
  private durableConfigDir(): string {
    if (isDev) return path.join(this.resolveStackDir(), "config");
    return path.join(process.env.ProgramData || "C:\\ProgramData", "TallyBridge", "config");
  }

  private stackConfigDir(): string {
    return path.join(this.resolveStackDir(), "config");
  }

  /** Recursive copy, used only for the small config tree (.env + 3 rendered files). */
  private mirrorDir(from: string, to: string) {
    if (!fs.existsSync(from) || path.resolve(from) === path.resolve(to)) return;
    fs.mkdirSync(to, { recursive: true });
    for (const entry of fs.readdirSync(from, { withFileTypes: true })) {
      const src = path.join(from, entry.name);
      const dst = path.join(to, entry.name);
      if (entry.isDirectory()) this.mirrorDir(src, dst);
      else fs.copyFileSync(src, dst);
    }
  }

  private readEnvFile(file: string): StackEnv {
    const out: StackEnv = {};
    for (const line of fs.readFileSync(file, "utf8").split(/\r?\n/)) {
      const m = line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/);
      if (m) out[m[1]] = m[2].trim();
    }
    return out;
  }

  /**
   * One-shot probe. Used to adopt an already-running stack instead of spawning a
   * duplicate that would fail to bind and then be "restarted" three times: that
   * happens in dev (where start.ps1 is run by hand) and after a crash that left
   * the children alive. Making start() idempotent is cheaper than trying to
   * detect those cases.
   */
  private isPortOpen(port: number): Promise<boolean> {
    return new Promise((resolve) => {
      const socket = createConnection({ host: "127.0.0.1", port });
      const done = (open: boolean) => {
        socket.destroy();
        resolve(open);
      };
      socket.once("connect", () => done(true));
      socket.once("error", () => done(false));
      setTimeout(() => done(false), 1500);
    });
  }

  /** Resolves once the port accepts a TCP connection, or rejects on timeout. */
  private waitForPort(port: number, label: string): Promise<void> {
    const deadline = Date.now() + READY_TIMEOUT_MS;
    return new Promise((resolve, reject) => {
      const attempt = () => {
        const socket = createConnection({ host: "127.0.0.1", port });
        socket.once("connect", () => {
          socket.destroy();
          resolve();
        });
        socket.once("error", () => {
          socket.destroy();
          if (Date.now() > deadline) {
            reject(new Error(`${label} did not open port ${port} within ${READY_TIMEOUT_MS}ms`));
            return;
          }
          setTimeout(attempt, READY_POLL_MS);
        });
      };
      attempt();
    });
  }

  private launch(
    name: string,
    exe: string,
    args: string[],
    extraPath: string[] = [],
    extraEnv: Record<string, string> = {},
  ): ChildProcess {
    const childEnv = {
      ...process.env,
      ...extraEnv,
      PATH: [...extraPath, process.env.PATH || ""].join(path.delimiter),
    };

    // Child output goes to FILE descriptors, never to inherited handles and never
    // to pipes. Inheriting our stdout would let the child hold that handle open,
    // so anything capturing this process blocks forever waiting for an EOF that
    // only arrives when the database stops. Plain 'ignore' avoids that but throws
    // away the one thing that explains a service refusing to start -- which cost
    // real time here when the backend exited 1 with nothing to show for it.
    const logDir = path.join(app.getPath("logs"), "stack");
    fs.mkdirSync(logDir, { recursive: true });
    const out = fs.openSync(path.join(logDir, `${name}.out.log`), "a");
    const err = fs.openSync(path.join(logDir, `${name}.err.log`), "a");

    const child = spawn(exe, args, {
      env: childEnv,
      windowsHide: true,
      stdio: ["ignore", out, err],
      detached: false,
    });

    child.on("error", (err) => {
      logger.error(`[stack] ${name} failed to spawn: ${err.message}`);
    });

    child.on("exit", (code, signal) => {
      const managed = this.processes.find((p) => p.name === name);
      if (!managed?.supervised) return; // deliberate shutdown
      logger.error(`[stack] ${name} exited unexpectedly (code=${code} signal=${signal})`);
      const attempts = (this.restartCounts.get(name) ?? 0) + 1;
      this.restartCounts.set(name, attempts);
      if (attempts > MAX_RESTARTS) {
        logger.error(`[stack] ${name} exceeded ${MAX_RESTARTS} restarts; leaving it down`);
        return;
      }
      logger.info(`[stack] restarting ${name} (attempt ${attempts}/${MAX_RESTARTS})`);
      managed.child = this.launch(name, exe, args, extraPath, extraEnv);
    });

    return child;
  }

  private track(name: string, child: ChildProcess) {
    this.processes.push({ name, child, supervised: true });
  }

  async start(): Promise<void> {
    if (this.started) return;

    this.stackDir = this.resolveStackDir();
    const durableDir = this.durableConfigDir();
    const stackCfgDir = this.stackConfigDir();

    // Authoritative copy in, so the scripts and the read below see this machine's
    // real secrets rather than whatever shipped in resources.
    this.mirrorDir(durableDir, stackCfgDir);

    const configPath = path.join(stackCfgDir, ".env");
    if (!fs.existsSync(configPath)) {
      // First run on this machine: no secrets, no cluster. Delegate to the
      // installer script rather than reimplementing initdb, role creation and a
      // 17-file schema apply in TypeScript -- that script is the one that has
      // actually been exercised end to end.
      logger.info("[stack] no config found; running first-time setup");
      this.bootstrap();
      // Preserve what it just generated: these secrets are the only way back
      // into the data directory.
      this.mirrorDir(stackCfgDir, durableDir);
    }

    this.env = this.readEnvFile(configPath);

    // VENDOR_DIR and BACKEND_DIR in .env are absolute paths recorded by whichever
    // machine last ran bootstrap. A packaged build must ignore them: on this box
    // they pointed at a dev checkout and at "C:\Program Files\TallyBridge Server"
    // left over from an earlier experiment, and on a client machine neither would
    // exist at all. The binaries we ship are the ones we support, so in a packaged
    // build they always win; only dev falls back to the recorded values.
    const vendor = isDev
      ? this.env.VENDOR_DIR || path.join(this.stackDir, "vendor")
      : path.join(this.stackDir, "vendor");
    const pgBin = path.join(vendor, "pgsql", "bin");
    const configDir = path.dirname(configPath);
    logger.info(`[stack] stackDir=${this.stackDir} vendor=${vendor} config=${configPath}`);

    const pgPort = Number(this.env.PGPORT || 5432);
    const restPort = Number(this.env.POSTGREST_PORT || 3000);
    const gwPort = Number(this.env.GATEWAY_PORT || 8000);
    const backendPort = Number(this.env.BACKEND_PORT || 3001);

    logger.info(`[stack] starting from ${this.stackDir} (data: ${this.env.PGDATA})`);

    // Pick the first candidate that actually contains a built backend. BACKEND_DIR
    // in .env is whatever the last bootstrap recorded -- on this machine it pointed
    // at "C:\Program Files\TallyBridge Server\backend", which does not exist, and
    // node then exited 1 on every restart attempt. Trust the filesystem, not the
    // config.
    const backendCandidates = [
      path.join(this.stackDir, "backend"),                        // packaged / staged
      ...(isDev ? [path.join(app.getAppPath(), "backend")] : []), // dev: the repo
      ...(this.env.BACKEND_DIR ? [this.env.BACKEND_DIR] : []),
    ];
    const backendDir = backendCandidates.find((d) =>
      fs.existsSync(path.join(d, "dist", "index.js")),
    );
    if (!backendDir) {
      throw new Error(
        `[stack] no built backend found. Looked in: ${backendCandidates.join(" ; ")}`,
      );
    }

    // The backend reads its Supabase target and API keys from the environment.
    // It must NOT fall back to backend\.env -- that file holds the CLOUD service
    // key and is deliberately stripped from the package, so on a client machine
    // there is nothing to fall back to. Note SUPABASE_URL_Client's capital C:
    // sync.ts:20 reads that exact spelling, and getting it wrong makes the MRP
    // report silently serve cloud data with every health check still passing.
    const gatewayUrl = `http://127.0.0.1:${gwPort}`;
    const apiKey = this.env.BACKEND_API_KEY || "localdevkey";
    this.backendUrl = `http://127.0.0.1:${backendPort}`;
    this.backendApiKey = apiKey;
    const backendEnv: Record<string, string> = {
      PORT: String(backendPort),
      SUPABASE_URL: gatewayUrl,
      SUPABASE_SERVICE_KEY: this.env.SERVICE_ROLE_KEY,
      API_KEY: apiKey,
      SUPABASE_URL_Client: gatewayUrl,
      SUPABASE_SERVICE_KEY_CLIENT: this.env.SERVICE_ROLE_KEY,
      API_KEY_CLIENT: apiKey,
    };

    // Order matters -- see the note at the top of the file.
    const services: Array<{
      name: string;
      port: number;
      exe: string;
      args: string[];
      extraPath?: string[];
      extraEnv?: Record<string, string>;
    }> = [
      {
        name: "postgres",
        port: pgPort,
        exe: path.join(pgBin, "postgres.exe"),
        args: ["-D", this.env.PGDATA, "-p", String(pgPort)],
      },
      {
        // pgBin on PATH for libpq.dll -- see the note at the top.
        name: "postgrest",
        port: restPort,
        exe: path.join(vendor, "postgrest", "postgrest.exe"),
        args: [path.join(configDir, "postgrest.conf")],
        extraPath: [pgBin],
      },
      {
        // Rewrites /rest/v1/* to PostgREST, which is the URL shape supabase-js
        // and the Flutter client both assume.
        name: "gateway",
        port: gwPort,
        exe: path.join(vendor, "caddy", "caddy.exe"),
        args: ["run", "--config", path.join(configDir, "Caddyfile"), "--adapter", "caddyfile"],
      },
      {
        name: "backend",
        port: backendPort,
        exe: path.join(vendor, "node", "node.exe"),
        args: [path.join(backendDir, "dist", "index.js")],
        extraEnv: backendEnv,
      },
    ];

    for (const svc of services) {
      if (await this.isPortOpen(svc.port)) {
        logger.info(`[stack] ${svc.name} already listening on ${svc.port}; adopting it`);
        continue;
      }
      if (!fs.existsSync(svc.exe)) {
        throw new Error(`[stack] ${svc.name} binary missing: ${svc.exe}`);
      }
      this.track(
        svc.name,
        this.launch(svc.name, svc.exe, svc.args, svc.extraPath ?? [], svc.extraEnv ?? {}),
      );
      await this.waitForPort(svc.port, svc.name);
      logger.info(`[stack] ${svc.name} ready on ${svc.port}`);
    }

    this.started = true;
    logger.info("[stack] all services up");
  }

  private bootstrap() {
    const dataDir = path.join(
      process.env.ProgramData || "C:\\ProgramData",
      "TallyBridge",
      "data",
    );
    // install.ps1 checks for node on PATH and refuses without it, but a client
    // machine has no reason to have Node installed -- that is why vendor\node is
    // bundled. Put it on PATH for the child rather than patching the check, so the
    // script stays runnable by hand as a repair.
    const bundledNode = path.join(
      this.stackDir, "vendor", "node",
    );
    const result = spawnSync(
      "powershell.exe",
      [
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy", "Bypass",
        "-File", path.join(this.stackDir, "install.ps1"),
        "-NoServices",
        "-SkipVendor",
        "-DataDir", dataDir,
      ],
      {
        cwd: this.stackDir,
        windowsHide: true,
        encoding: "utf8",
        timeout: 20 * 60 * 1000,
        env: {
          ...process.env,
          PATH: [bundledNode, process.env.PATH || ""].join(path.delimiter),
        },
      },
    );
    if (result.status !== 0) {
      logger.error(`[stack] first-time setup failed (${result.status}): ${result.stderr ?? ""}`);
      throw new Error("Local stack setup failed; see the TallyBridge log.");
    }
    logger.info("[stack] first-time setup complete");
  }

  /**
   * Reverse start order, and Postgres last so the others release their
   * connections before the checkpoint. `pg_ctl -m fast stop` rather than a kill:
   * an unclean postmaster exit forces crash recovery on the next launch, which on
   * a database this size is a visibly slow start.
   */
  async stop(): Promise<void> {
    if (!this.started) return;
    this.started = false;

    for (const p of this.processes) p.supervised = false;

    for (const name of ["backend", "gateway", "postgrest"]) {
      const managed = this.processes.find((p) => p.name === name);
      if (managed && !managed.child.killed) {
        managed.child.kill();
        logger.info(`[stack] stopped ${name}`);
      }
    }

    const vendor = this.env.VENDOR_DIR || path.join(this.stackDir, "vendor");
    try {
      spawnSync(
        path.join(vendor, "pgsql", "bin", "pg_ctl.exe"),
        ["-D", this.env.PGDATA, "-m", "fast", "-w", "stop"],
        { windowsHide: true, timeout: 60_000, stdio: "ignore" },
      );
      logger.info("[stack] stopped postgres");
    } catch (err) {
      logger.error(`[stack] pg_ctl stop failed: ${err instanceof Error ? err.message : err}`);
      this.processes.find((p) => p.name === "postgres")?.child.kill();
    }

    this.processes = [];
  }

  isRunning(): boolean {
    return this.started;
  }
}
