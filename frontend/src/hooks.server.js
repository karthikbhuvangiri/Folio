/**
 * hooks.server.js
 * Server-side hook that proxies /api/* requests to the backend service.
 *
 * In development, Vite's server.proxy handles this.
 * In production (Docker), there's no Vite dev server — SvelteKit's
 * Node adapter serves the app directly. This hook forwards API
 * requests to the backend container.
 */

const BACKEND_URL = process.env.BACKEND_URL || 'http://localhost:8000';
const DEMO_MODE = String(process.env.DEMO_MODE || '').trim().toLowerCase();
const ALLOWED_HOSTS = new Set(
    (process.env.FRONTEND_ALLOWED_HOSTS || process.env.RENDER_EXTERNAL_HOSTNAME || 'localhost,127.0.0.1,::1')
        .split(',')
        .map((host) => host.trim().toLowerCase())
        .filter(Boolean)
);
const ALLOW_ANY_HOST = ALLOWED_HOSTS.has('*') || ['1', 'true', 'yes', 'on'].includes(DEMO_MODE);

function normalizeHost(hostHeader) {
    const host = (hostHeader || '').trim().toLowerCase();
    if (!host) return '';

    if (host.startsWith('[')) {
        const end = host.indexOf(']');
        return end > 0 ? host.slice(1, end) : host;
    }

    return host.split(':')[0];
}

function isAllowedHost(host) {
    if (ALLOW_ANY_HOST) return true;
    return ALLOWED_HOSTS.has(host);
}

/** Headers that should NOT be forwarded to the backend */
const HOP_BY_HOP = new Set([
    'connection',
    'keep-alive',
    'transfer-encoding',
    'te',
    'trailer',
    'upgrade',
    'host',
]);

export async function handle({ event, resolve }) {
    const host = normalizeHost(event.request.headers.get('host'));
    if (!isAllowedHost(host)) {
        return new Response('Not found', { status: 404 });
    }

    // Only intercept /api/* requests
    if (!event.url.pathname.startsWith('/api')) {
        return resolve(event);
    }

    const targetUrl = `${BACKEND_URL}${event.url.pathname}${event.url.search}`;

    try {
        // Build headers — forward everything except hop-by-hop
        const forwardHeaders = new Headers();
        for (const [key, value] of event.request.headers.entries()) {
            if (!HOP_BY_HOP.has(key.toLowerCase())) {
                forwardHeaders.set(key, value);
            }
        }

        // Determine body handling
        const method = event.request.method;
        let body = null;
        if (method !== 'GET' && method !== 'HEAD') {
            body = await event.request.arrayBuffer();
        }

        // Enroll/sync can take 6+ minutes (LLM categorization batches).
        // Use a generous timeout for long-running endpoints, default for others.
        const isLongRunning = targetUrl.includes('/api/enroll') || targetUrl.includes('/api/sync');
        const timeoutMs = isLongRunning ? 10 * 60 * 1000 : 2 * 60 * 1000; // 10min / 2min

        const backendResponse = await fetch(targetUrl, {
            method,
            headers: forwardHeaders,
            body,
            signal: AbortSignal.timeout(timeoutMs),
        });

        // Build response — forward backend headers
        const responseHeaders = new Headers();
        for (const [key, value] of backendResponse.headers.entries()) {
            if (!HOP_BY_HOP.has(key.toLowerCase())) {
                responseHeaders.set(key, value);
            }
        }

        return new Response(backendResponse.body, {
            status: backendResponse.status,
            statusText: backendResponse.statusText,
            headers: responseHeaders,
        });
    } catch (err) {
        console.error(`[hooks.server.js] Proxy error for ${targetUrl}:`, err.message);
        return new Response(
            JSON.stringify({ detail: 'Backend unavailable' }),
            {
                status: 502,
                headers: { 'Content-Type': 'application/json' },
            }
        );
    }
}

export async function handleFetch({ request, fetch }) {
    const url = new URL(request.url);

    if (!url.pathname.startsWith('/api')) {
        return fetch(request);
    }

    const targetUrl = `${BACKEND_URL}${url.pathname}${url.search}`;
    const headers = new Headers(request.headers);
    if (!headers.has('X-API-Key')) {
        const apiKey = process.env.VITE_API_KEY || process.env.Folio_API_KEY || process.env.FOLIO_API_KEY || '';
        if (apiKey) headers.set('X-API-Key', apiKey);
    }

    const init = {
        method: request.method,
        headers,
    };
    if (request.method !== 'GET' && request.method !== 'HEAD') {
        init.body = await request.arrayBuffer();
    }

    return fetch(targetUrl, init);
}
