import express from 'express';
import type { Request, Response } from 'express';
import { spawn } from 'node:child_process';
import type { ChildProcess } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const PORT = Number(process.env.PORT || 3000);
const PYTHON_PORT = Number(process.env.PYTHON_HUB_PORT || 8001);
const PYTHON_ORIGIN = `http://127.0.0.1:${PYTHON_PORT}`;
const IS_PROD = process.env.NODE_ENV === 'production';

let pythonProc: ChildProcess | null = null;

function startPythonHub(): void {
  if (pythonProc) return;
  pythonProc = spawn(
    'python3',
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

  pythonProc.on('exit', (code) => {
    console.warn(`[python-hub] exited with code ${code}`);
    pythonProc = null;
  });
}

async function waitForPythonHub(retries = 25): Promise<boolean> {
  for (let i = 0; i < retries; i++) {
    try {
      const res = await fetch(`${PYTHON_ORIGIN}/api/overview?refresh=false`, {
        signal: AbortSignal.timeout(500),
      });
      if (res.ok) return true;
    } catch {
      await new Promise((r) => setTimeout(r, 120));
    }
  }
  return false;
}

async function proxyToPython(req: Request, res: Response): Promise<void> {
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
  startPythonHub();
  await waitForPythonHub();

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
