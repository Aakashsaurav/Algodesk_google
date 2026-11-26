import express from 'express';
import type { Request, Response } from 'express';
import { spawn } from 'node:child_process';
import type { ChildProcess } from 'node:child_process';
import fs from 'node:fs';
import http from 'node:http';
import net from 'node:net';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

function readCliArg(flag: string): string | undefined {
  const args = process.argv.slice(2);
  for (let i = 0; i < args.length; i++) {
    const arg = args[i];
    if (arg === flag && args[i + 1]) {
      return args[i + 1];
    }
    if (arg.startsWith(`${flag}=`)) {
      return arg.slice(flag.length + 1);
    }
  }
  return undefined;
}

const IS_AISTUDIO_PREVIEW = Boolean(
  process.env.APPLET_ID || process.env.NGINX_PORT || process.env.DEFAULT_APP_PORT
);
const PORT =
  Number(
    readCliArg('--port') ||
      process.env.ALGODESK_PORT ||
      (IS_AISTUDIO_PREVIEW
        ? process.env.DEFAULT_APP_PORT || '3000'
        : process.env.PORT || '3000')
  ) || 3000;
const IS_CLOUD_RUNTIME = Boolean(
  IS_AISTUDIO_PREVIEW ||
    process.env.K_SERVICE ||
    process.env.CLOUD_RUN_JOB ||
    process.env.RENDER ||
    process.env.FLY_APP_NAME
);
const ALLOW_REMOTE = ['1', 'true', 'yes', 'on'].includes(
  String(process.env.ALGODESK_ALLOW_REMOTE || '').trim().toLowerCase()
);
const REQUESTED_HOST = readCliArg('--host') || process.env.HOST;
const LOOPBACK_HOSTS = new Set(['127.0.0.1', 'localhost', '::1']);
let HOST = REQUESTED_HOST || (IS_CLOUD_RUNTIME || ALLOW_REMOTE ? '0.0.0.0' : '127.0.0.1');
if (!LOOPBACK_HOSTS.has(HOST) && !ALLOW_REMOTE && !IS_CLOUD_RUNTIME) {
  console.warn(
    `[security] Refusing non-loopback bind '${HOST}' without ALGODESK_ALLOW_REMOTE=1; falling back to 127.0.0.1.`
  );
  HOST = '127.0.0.1';
} else if (!LOOPBACK_HOSTS.has(HOST)) {
  console.warn(
    `[security] Binding AlgoDesk web bridge on non-loopback interface ${HOST}:${PORT}. Ensure TLS reverse proxy and operator authentication are active.`
  );
}
const BASE_PYTHON_PORT = Number(process.env.PYTHON_HUB_PORT || 8001);
let activePythonPort = BASE_PYTHON_PORT;
const IS_PROD = process.env.NODE_ENV === 'production';

let pythonProc: ChildProcess | null = null;
let pythonReadyPromise: Promise<boolean> | null = null;

function getPythonOrigin(): string {
  return `http://127.0.0.1:${activePythonPort}`;
}

function resolvePythonBin(): string {
  const explicit = process.env.ALGODESK_PYTHON?.trim() || process.env.PYTHON_BIN?.trim();
  if (explicit) {
    return explicit;
  }
  const candidates: string[] = [];
  const activeVenv = process.env.VIRTUAL_ENV?.trim();
  if (activeVenv) {
    candidates.push(
      path.join(activeVenv, 'bin', 'python3'),
      path.join(activeVenv, 'bin', 'python'),
      path.join(activeVenv, 'Scripts', 'python.exe')
    );
  }
  const condaPrefix = process.env.CONDA_PREFIX?.trim();
  if (condaPrefix) {
    candidates.push(
      path.join(condaPrefix, 'bin', 'python3'),
      path.join(condaPrefix, 'bin', 'python'),
      path.join(condaPrefix, 'python.exe')
    );
  }
  for (const dirName of ['.venv', 'venv', 'env']) {
    candidates.push(
      path.join(__dirname, dirName, 'bin', 'python3'),
      path.join(__dirname, dirName, 'bin', 'python'),
      path.join(__dirname, dirName, 'Scripts', 'python.exe')
    );
  }
  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) {
      return candidate;
    }
  }
  const pathDirs = (process.env.PATH || '').split(path.delimiter).filter(Boolean);
  for (const binName of process.platform === 'win32' ? ['python.exe', 'python3.exe'] : ['python3', 'python']) {
    for (const dir of pathDirs) {
      const full = path.join(dir, binName);
      if (fs.existsSync(full)) {
        return full;
      }
    }
  }
  return 'python3';
}

function startPythonHub(port = activePythonPort): void {
  if (pythonProc && pythonProc.exitCode === null && !pythonProc.killed) return;
  activePythonPort = port;
  const pythonBin = resolvePythonBin();
  console.log(`[python-hub] starting with interpreter: ${pythonBin}`);
  pythonProc = spawn(
    pythonBin,
    ['-m', 'web.local_server', '--host', '127.0.0.1', '--port', String(activePythonPort), '--no-browser'],
    {
      cwd: __dirname,
      stdio: ['ignore', 'pipe', 'pipe'],
      env: { ...process.env, PYTHONUNBUFFERED: '1' },
    }
  );

  pythonProc.stdout?.on('data', (chunk: Buffer) => {
    const line = chunk.toString().trim();
    if (line) console.log(`[python-hub] ${line}`);
  });

  pythonProc.stderr?.on('data', (chunk: Buffer) => {
    const line = chunk.toString().trim();
    if (line) console.error(`[python-hub] ${line}`);
  });

  pythonProc.on('error', (err) => {
    console.error(`[python-hub] spawn error: ${err.message}`);
    pythonProc = null;
  });

  pythonProc.on('exit', (code) => {
    console.warn(`[python-hub] exited with code ${code}`);
    pythonProc = null;
  });
}

async function waitForPythonHub(retries = 25): Promise<boolean> {
  startPythonHub(activePythonPort);
  for (let i = 0; i < retries; i++) {
    if (!pythonProc || pythonProc.exitCode !== null || pythonProc.killed) {
      activePythonPort += 1;
      startPythonHub(activePythonPort);
    }
    try {
      const res = await fetch(`${getPythonOrigin()}/api/health`, {
        signal: AbortSignal.timeout(400),
      });
      if (res.ok) {
        const body = (await res.json().catch(() => null)) as { phase?: number; live_terminal?: boolean } | null;
        if (Number(body?.phase ?? 0) >= 5 && body?.live_terminal === true) {
          return true;
        }
        // A stale pre-Live-Terminal process is bound to this port; move cleanly to the next port.
        activePythonPort += 1;
        if (pythonProc) {
          try {
            pythonProc.kill('SIGTERM');
          } catch {
            // ignore
          }
          pythonProc = null;
        }
        startPythonHub(activePythonPort);
      }
    } catch {
      await new Promise((r) => setTimeout(r, 120));
    }
  }
  return false;
}

async function ensurePythonReady(): Promise<boolean> {
  if (!pythonProc || pythonProc.exitCode !== null || pythonProc.killed) {
    pythonReadyPromise = null;
  }
  if (!pythonReadyPromise) {
    pythonReadyPromise = waitForPythonHub().finally(() => {
      pythonReadyPromise = null;
    });
  }
  return pythonReadyPromise;
}

async function proxyToPython(req: Request, res: Response): Promise<void> {
  await ensurePythonReady();
  const targetUrl = `${getPythonOrigin()}${req.originalUrl}`;
  try {
    const headers: Record<string, string> = {};
    if (req.headers['content-type']) {
      headers['content-type'] = String(req.headers['content-type']);
    }
    if (req.headers['authorization']) {
      headers['authorization'] = String(req.headers['authorization']);
    }
    if (req.headers['accept']) {
      headers['accept'] = String(req.headers['accept']);
    }
    if (req.headers['cookie']) {
      headers['cookie'] = String(req.headers['cookie']);
    }
    const clientIp = req.headers['x-forwarded-for'] || req.socket.remoteAddress;
    if (clientIp) {
      headers['x-forwarded-for'] = String(clientIp);
    }

    const method = req.method.toUpperCase();
    const hasBody = !['GET', 'HEAD'].includes(method);
    const body = hasBody && req.body && Object.keys(req.body).length > 0 ? JSON.stringify(req.body) : undefined;
    if (body && !headers['content-type']) {
      headers['content-type'] = 'application/json';
    }

    const isLongRoute =
      req.originalUrl.startsWith('/api/streak/backtest') ||
      req.originalUrl.startsWith('/api/streak/technicals') ||
      req.originalUrl.startsWith('/api/analytics/screener') ||
      req.originalUrl.startsWith('/api/analytics/backtest');
    const upstream = await fetch(targetUrl, {
      method,
      headers,
      body,
      signal: req.originalUrl.startsWith('/api/live/stream')
        ? undefined
        : AbortSignal.timeout(isLongRoute ? 60000 : 15000),
    });

    const retryAfter = upstream.headers.get('retry-after');
    if (retryAfter) {
      res.setHeader('Retry-After', retryAfter);
    }

    const contentType = upstream.headers.get('content-type') || 'application/json';
    if (contentType.includes('text/event-stream') && upstream.body) {
      res.status(upstream.status);
      res.setHeader('Content-Type', 'text/event-stream; charset=utf-8');
      res.setHeader('Cache-Control', 'no-cache');
      res.setHeader('Connection', 'keep-alive');
      res.flushHeaders();
      const reader = upstream.body.getReader();
      req.on('close', () => {
        void reader.cancel().catch(() => {});
      });
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        res.write(Buffer.from(value));
      }
      res.end();
      return;
    }

    const text = await upstream.text();
    res.status(upstream.status).setHeader('Content-Type', contentType).send(text);
  } catch (err) {
    const message = err instanceof Error ? err.message : 'Python hub unavailable';
    if (!res.headersSent) {
      res.status(502).json({ detail: `Python broker hub unreachable: ${message}` });
    }
  }
}

async function bootstrap(): Promise<void> {
  // Spawn Python hub immediately in the background without blocking port 3000 bind.
  startPythonHub();

  const app = express();
  app.use(express.json({ limit: '1mb' }));

  app.all('/api/*', (req, res) => {
    void proxyToPython(req, res);
  });

  app.get('/auth/*', (req, res) => {
    void proxyToPython(req, res);
  });

  const server = http.createServer(app);
  let viteServer: { middlewares: express.RequestHandler; close: () => Promise<void> } | null = null;

  if (!IS_PROD) {
    const disableHmr = process.env.DISABLE_HMR === 'true';
    if (disableHmr) {
      const noHmrClientShim = `
const sheetsMap = new Map();
export function updateStyle(id, content) {
  if (typeof document === 'undefined') return;
  let style = sheetsMap.get(id);
  if (!style) {
    style = document.createElement('style');
    style.setAttribute('type', 'text/css');
    style.setAttribute('data-vite-dev-id', id);
    style.textContent = content;
    document.head.appendChild(style);
    sheetsMap.set(id, style);
  } else if (style.textContent !== content) {
    style.textContent = content;
  }
}
export function removeStyle(id) {
  const style = sheetsMap.get(id);
  if (style) {
    style.remove();
    sheetsMap.delete(id);
  }
}
export function createHotContext() {
  return {
    data: {},
    accept() {},
    acceptExports() {},
    dispose() {},
    prune() {},
    invalidate() {},
    on() {},
    off() {},
    send() {},
  };
}
export function injectQuery(url, queryToInject) {
  if (!url || (url[0] !== '.' && url[0] !== '/')) return url;
  const pathname = url.replace(/[?#].*$/, '');
  const { search, hash } = new URL(url, 'http://vite.dev');
  return \`\${pathname}?\${queryToInject}\${search ? '&' + search.slice(1) : ''}\${hash || ''}\`;
}
export class ErrorOverlay extends HTMLElement {}
`;
      app.get('/@vite/client', (_req, res) => {
        res.setHeader('Content-Type', 'application/javascript; charset=utf-8');
        res.setHeader('Cache-Control', 'no-cache');
        res.send(noHmrClientShim);
      });
    }

    const vitePromise = import('vite').then(({ createServer: createViteServer }) =>
      createViteServer({
        server: {
          middlewareMode: true,
          hmr: disableHmr ? false : { server },
          ws: disableHmr ? false : undefined,
        },
        appType: 'spa',
      })
    );
    vitePromise
      .then((vite) => {
        viteServer = vite;
      })
      .catch((err) => {
        console.error('[vite] failed to initialize dev middleware:', err);
      });

    app.use(async (req, res, next) => {
      try {
        const vite = viteServer ?? (await vitePromise);
        vite.middlewares(req, res, next);
      } catch (err) {
        next(err);
      }
    });
  } else {
    const distDir = path.resolve(__dirname, 'dist');
    app.use(express.static(distDir));
    app.get('*', (_req, res) => {
      res.sendFile(path.join(distDir, 'index.html'));
    });
  }

  server.on('error', (err) => {
    console.error(`[server] failed to bind http://${HOST}:${PORT}:`, err);
    process.exit(1);
  });

  // Bind HTTP port immediately so platform health checks pass without waiting on Vite cold boot.
  server.listen(PORT, HOST, () => {
    console.log(`AlgoDesk server listening on http://${HOST}:${PORT}`);
  });

  // Tunnel /ws/* WebSocket upgrades directly to the Python broker hub (FastAPI or stdlib WS)
  server.on('upgrade', (req, socket, head) => {
    const reqUrl = req.url || '/';
    if (!reqUrl.startsWith('/ws/')) {
      return;
    }
    void (async () => {
      try {
        await ensurePythonReady();
        const upstream = net.createConnection({ host: '127.0.0.1', port: activePythonPort }, () => {
          const rawHeaders: string[] = [];
          for (const [k, v] of Object.entries(req.headers)) {
            if (v === undefined) continue;
            if (Array.isArray(v)) {
              for (const val of v) rawHeaders.push(`${k}: ${val}`);
            } else {
              rawHeaders.push(`${k}: ${v}`);
            }
          }
          const reqHead = `GET ${reqUrl} HTTP/${req.httpVersion || '1.1'}\r\n${rawHeaders.join('\r\n')}\r\n\r\n`;
          upstream.write(reqHead);
          if (head && head.length > 0) {
            upstream.write(head);
          }
          socket.pipe(upstream);
          upstream.pipe(socket);
        });
        upstream.on('error', () => {
          socket.destroy();
        });
        socket.on('error', () => {
          upstream.destroy();
        });
      } catch {
        socket.destroy();
      }
    })();
  });

  let shuttingDown = false;
  const shutdown = () => {
    if (shuttingDown) return;
    shuttingDown = true;
    const procToKill = pythonProc;
    if (procToKill) {
      try {
        procToKill.kill('SIGTERM');
      } catch {
        // ignore
      }
      setTimeout(() => {
        if (procToKill.exitCode === null) {
          try {
            procToKill.kill('SIGKILL');
          } catch {
            // ignore
          }
        }
      }, 250).unref();
    }
    void viteServer?.close().catch(() => {});
    server.closeAllConnections?.();
    server.close(() => process.exit(0));
    setTimeout(() => process.exit(0), 600).unref();
  };
  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);
  process.on('exit', () => {
    if (pythonProc && pythonProc.exitCode === null && !pythonProc.killed) {
      try {
        pythonProc.kill('SIGTERM');
      } catch {
        // ignore
      }
    }
  });
}

void bootstrap();
