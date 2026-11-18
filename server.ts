import express from 'express';
import type { Request, Response } from 'express';
import { spawn } from 'node:child_process';
import type { ChildProcess } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const PORT = Number(process.env.PORT || 3000);
const BASE_PYTHON_PORT = Number(process.env.PYTHON_HUB_PORT || 8001);
let activePythonPort = BASE_PYTHON_PORT;
const IS_PROD = process.env.NODE_ENV === 'production';

let pythonProc: ChildProcess | null = null;
let pythonReadyPromise: Promise<boolean> | null = null;

function getPythonOrigin(): string {
  return `http://127.0.0.1:${activePythonPort}`;
}

function resolvePythonBin(): string {
  if (process.env.PYTHON_BIN?.trim()) {
    return process.env.PYTHON_BIN.trim();
  }
  const venvCandidates = [
    path.join(__dirname, '.venv', 'bin', 'python3'),
    path.join(__dirname, '.venv', 'bin', 'python'),
    path.join(__dirname, '.venv', 'Scripts', 'python.exe'),
  ];
  for (const candidate of venvCandidates) {
    if (fs.existsSync(candidate)) {
      return candidate;
    }
  }
  return 'python3';
}

function startPythonHub(port = activePythonPort): void {
  if (pythonProc && pythonProc.exitCode === null && !pythonProc.killed) return;
  activePythonPort = port;
  const pythonBin = resolvePythonBin();
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
    try {
      const res = await fetch(`${getPythonOrigin()}/api/health`, {
        signal: AbortSignal.timeout(400),
      });
      if (res.ok) {
        const body = (await res.json().catch(() => null)) as { phase?: number } | null;
        if (body?.phase === 3) {
          return true;
        }
        // A stale pre-Phase 3 process is bound to this port; move cleanly to the next port.
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

    const method = req.method.toUpperCase();
    const hasBody = !['GET', 'HEAD'].includes(method);
    const body = hasBody && req.body && Object.keys(req.body).length > 0 ? JSON.stringify(req.body) : undefined;
    if (body && !headers['content-type']) {
      headers['content-type'] = 'application/json';
    }

    const upstream = await fetch(targetUrl, {
      method,
      headers,
      body,
      signal: AbortSignal.timeout(15000),
    });

    const contentType = upstream.headers.get('content-type') || 'application/json';
    const text = await upstream.text();
    res.status(upstream.status).setHeader('Content-Type', contentType).send(text);
  } catch (err) {
    const message = err instanceof Error ? err.message : 'Python hub unavailable';
    res.status(502).json({ detail: `Python broker hub unreachable: ${message}` });
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

  if (!IS_PROD) {
    const { createServer: createViteServer } = await import('vite');
    const vite = await createViteServer({
      server: { middlewareMode: true },
      appType: 'spa',
    });
    app.use(vite.middlewares);
  } else {
    const distDir = path.resolve(__dirname, 'dist');
    app.use(express.static(distDir));
    app.get('*', (_req, res) => {
      res.sendFile(path.join(distDir, 'index.html'));
    });
  }

  const server = app.listen(PORT, '0.0.0.0', () => {
    console.log(`AlgoDesk server listening on http://0.0.0.0:${PORT}`);
  });

  const shutdown = () => {
    if (pythonProc) {
      pythonProc.kill('SIGTERM');
    }
    server.close(() => process.exit(0));
  };
  process.on('SIGINT', shutdown);
  process.on('SIGTERM', shutdown);
}

void bootstrap();
