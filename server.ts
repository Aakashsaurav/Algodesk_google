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
const PYTHON_PORT = Number(process.env.PYTHON_HUB_PORT || 8001);
const PYTHON_ORIGIN = `http://127.0.0.1:${PYTHON_PORT}`;
const IS_PROD = process.env.NODE_ENV === 'production';

let pythonProc: ChildProcess | null = null;
let pythonReadyPromise: Promise<boolean> | null = null;

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

function startPythonHub(): void {
  if (pythonProc && pythonProc.exitCode === null && !pythonProc.killed) return;
  const pythonBin = resolvePythonBin();
  pythonProc = spawn(
    pythonBin,
    ['-m', 'web.local_server', '--host', '127.0.0.1', '--port', String(PYTHON_PORT), '--no-browser'],
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

async function waitForPythonHub(retries = 20): Promise<boolean> {
  startPythonHub();
  for (let i = 0; i < retries; i++) {
    try {
      const res = await fetch(`${PYTHON_ORIGIN}/api/health`, {
        signal: AbortSignal.timeout(400),
      });
      if (res.ok) return true;
    } catch {
      await new Promise((r) => setTimeout(r, 100));
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
  const targetUrl = `${PYTHON_ORIGIN}${req.originalUrl}`;
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
