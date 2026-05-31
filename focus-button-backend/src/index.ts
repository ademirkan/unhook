// Focus Button Backend — Cloudflare Worker + Durable Object (WebSocket push)
//
// HTTP endpoints (on the Worker):
//   POST /press               — Button reports a press (UNCHANGED behavior)
//   GET  /state               — Source-of-truth state (UNCHANGED, still used by clients)
//   GET  /dispense-pending    — Dispenser polls (UNCHANGED for now)
//   POST /dispense-ack        — Dispenser confirms (UNCHANGED)
//   GET  /health              — Sanity check (UNCHANGED)
//   GET  /ws                  — NEW: WebSocket upgrade, proxied to the Hub Durable Object
//
// The Durable Object ("Hub") holds all live WebSocket connections and broadcasts
// events (session_started, session_ended) to them in real time.

import { DurableObject } from 'cloudflare:workers';

// ===== Types =====

interface Env {
	DB: D1Database;
	HUB: DurableObjectNamespace<Hub>;
}

interface ActiveSession {
	id: number;
	startedAt: number;
	endsAt: number;
	durationMinutes: number;
}

// ===== Config =====

const SHARED_SECRET = 'PASTE-YOUR-OPENSSL-OUTPUT-HERE';
const DEFAULT_DURATION_MINUTES = 60;
const DISPENSE_WINDOW_SECONDS = 30;

// ===== Worker (HTTP router) =====

export default {
	async fetch(request: Request, env: Env): Promise<Response> {
		const url = new URL(request.url);
		const method = request.method;

		// Public health check (no auth)
		if (method === 'GET' && url.pathname === '/health') {
			return json({ ok: true });
		}

		// ---- Auth ----
		// For normal HTTP requests, the token is in the Authorization header.
		// For the WebSocket upgrade, browsers can't set custom headers, so we
		// also accept the token as a ?token= query param.
		const headerAuth = request.headers.get('Authorization');
		const queryToken = url.searchParams.get('token');
		const authorized = headerAuth === `Bearer ${SHARED_SECRET}` || queryToken === SHARED_SECRET;

		if (!authorized) {
			return json({ error: 'unauthorized' }, 401);
		}

		// ---- WebSocket upgrade: hand off to the Hub Durable Object ----
		if (url.pathname === '/ws') {
			if (request.headers.get('Upgrade') !== 'websocket') {
				return json({ error: 'expected websocket upgrade' }, 426);
			}
			const id = env.HUB.idFromName('global');
			const stub = env.HUB.get(id);
			return stub.fetch(request);
		}

		// ---- Normal HTTP routes ----
		try {
			if (method === 'POST' && url.pathname === '/press') {
				return await handlePress(request, env);
			}
			if (method === 'GET' && url.pathname === '/state') {
				return await handleState(env);
			}
			if (method === 'GET' && url.pathname === '/dispense-pending') {
				return await handleDispensePending(env);
			}
			if (method === 'POST' && url.pathname === '/dispense-ack') {
				return await handleDispenseAck(request, env);
			}
			return json({ error: 'not found' }, 404);
		} catch (err) {
			const message = err instanceof Error ? err.message : 'unknown error';
			console.error('Handler error:', err);
			return json({ error: message }, 500);
		}
	},
} satisfies ExportedHandler<Env>;

// ===== Durable Object: Hub =====
//
// Holds all connected WebSocket clients and broadcasts events to them.
// Uses the Hibernation WebSocket API so the DO sleeps when idle (no billing
// for idle time) while clients stay connected.

export class Hub extends DurableObject<Env> {
	async fetch(request: Request): Promise<Response> {
		const url = new URL(request.url);

		// Internal broadcast endpoint, called by the Worker (not by clients).
		if (url.pathname === '/broadcast') {
			const body = await request.text();
			this.broadcast(body);
			return new Response('ok');
		}

		// WebSocket upgrade from a client.
		if (request.headers.get('Upgrade') === 'websocket') {
			const pair = new WebSocketPair();
			const client = pair[0];
			const server = pair[1];

			// Accept with hibernation support. The DO can be evicted from memory
			// while this socket stays open; it wakes when a message arrives.
			this.ctx.acceptWebSocket(server);

			return new Response(null, { status: 101, webSocket: client });
		}

		return new Response('not found', { status: 404 });
	}

	// Called automatically when a client sends a message.
	// We use this to handle ping/keepalive from clients.
	async webSocketMessage(ws: WebSocket, message: string | ArrayBuffer): Promise<void> {
		// Clients may send "ping"; reply "pong" so they can detect a live link.
		if (message === 'ping') {
			ws.send('pong');
		}
		// We don't expect other client messages in this design; ignore them.
	}

	// Called automatically when a client disconnects.
	async webSocketClose(ws: WebSocket, code: number, reason: string, wasClean: boolean): Promise<void> {
		// Hibernation API tracks sockets for us via ctx.getWebSockets();
		// nothing to clean up manually. Log for debugging.
		console.log(`WebSocket closed: code=${code} reason=${reason} clean=${wasClean}`);
	}

	async webSocketError(ws: WebSocket, error: unknown): Promise<void> {
		console.error('WebSocket error:', error);
	}

	// Send a message to every connected client.
	broadcast(message: string): void {
		const sockets = this.ctx.getWebSockets();
		for (const ws of sockets) {
			try {
				ws.send(message);
			} catch (e) {
				// Socket is dead; close it. getWebSockets() will stop returning it.
				try {
					ws.close(1011, 'broadcast failed');
				} catch {
					// ignore
				}
			}
		}
	}
}

// ===== Helpers =====

function json(body: unknown, status = 200): Response {
	return new Response(JSON.stringify(body), {
		status,
		headers: { 'Content-Type': 'application/json' },
	});
}

function now(): number {
	return Math.floor(Date.now() / 1000);
}

async function logEvent(env: Env, type: string, payload: Record<string, unknown> = {}): Promise<void> {
	await env.DB.prepare('INSERT INTO events (type, payload, timestamp) VALUES (?, ?, ?)').bind(type, JSON.stringify(payload), now()).run();
}

async function getActiveSession(env: Env): Promise<ActiveSession | null> {
	const result = await env.DB.prepare(
		"SELECT id, payload, timestamp FROM events WHERE type = 'session_started' ORDER BY timestamp DESC LIMIT 1",
	).first<{ id: number; payload: string; timestamp: number }>();

	if (!result) return null;

	const payload = JSON.parse(result.payload) as { duration_minutes: number };
	const endsAt = result.timestamp + payload.duration_minutes * 60;

	if (endsAt > now()) {
		return {
			id: result.id,
			startedAt: result.timestamp,
			endsAt,
			durationMinutes: payload.duration_minutes,
		};
	}
	return null;
}

// Notify the Hub DO to broadcast an event to all connected clients.
async function notifyHub(env: Env, event: Record<string, unknown>): Promise<void> {
	try {
		const id = env.HUB.idFromName('global');
		const stub = env.HUB.get(id);
		await stub.fetch('https://hub/broadcast', {
			method: 'POST',
			body: JSON.stringify(event),
		});
	} catch (e) {
		// Broadcasting is best-effort. If it fails, clients will still catch up
		// via GET /state on their next poll/reconnect. Don't fail the request.
		console.error('notifyHub failed:', e);
	}
}

// ===== Handlers =====

async function handlePress(request: Request, env: Env): Promise<Response> {
	const body = (await request.json().catch(() => ({}))) as {
		duration_minutes?: number;
	};
	const duration = body.duration_minutes || DEFAULT_DURATION_MINUTES;

	// Always log the raw press
	await logEvent(env, 'button_press', { duration_minutes: duration });

	// Check for an active session (Option C: presses during a session are no-ops)
	const activeSession = await getActiveSession(env);

	if (activeSession) {
		return json({
			ok: true,
			action: 'logged_only',
			session_unchanged: true,
			minutes_remaining: Math.ceil((activeSession.endsAt - now()) / 60),
		});
	}

	// No active session: start one
	const startedAt = now();
	const endsAt = startedAt + duration * 60;
	await logEvent(env, 'session_started', { duration_minutes: duration });

	// NEW: push the event to all connected clients in real time
	await notifyHub(env, {
		type: 'session_started',
		ends_at: endsAt,
		duration_minutes: duration,
		started_at: startedAt,
	});

	return json({
		ok: true,
		action: 'session_started',
		duration_minutes: duration,
		ends_at: endsAt,
	});
}

async function handleState(env: Env): Promise<Response> {
	const session = await getActiveSession(env);

	if (!session) {
		return json({ should_block: false });
	}

	const minutesRemaining = Math.max(0, Math.ceil((session.endsAt - now()) / 60));

	return json({
		should_block: minutesRemaining > 0,
		minutes_remaining: minutesRemaining,
		started_at: session.startedAt,
		ends_at: session.endsAt,
		session_id: session.id,
	});
}

async function handleDispensePending(env: Env): Promise<Response> {
	const session = await env.DB.prepare(
		"SELECT id, timestamp FROM events WHERE type = 'session_started' ORDER BY timestamp DESC LIMIT 1",
	).first<{ id: number; timestamp: number }>();

	if (!session) {
		return json({ pending: false });
	}

	const ack = await env.DB.prepare("SELECT id FROM events WHERE type = 'dispense_ack' AND timestamp >= ? LIMIT 1")
		.bind(session.timestamp)
		.first();

	if (ack) {
		return json({ pending: false });
	}

	if (now() - session.timestamp > DISPENSE_WINDOW_SECONDS) {
		return json({ pending: false, reason: 'stale' });
	}

	return json({ pending: true, session_id: session.id });
}

async function handleDispenseAck(request: Request, env: Env): Promise<Response> {
	const body = (await request.json().catch(() => ({}))) as {
		session_id?: number;
	};
	await logEvent(env, 'dispense_ack', { session_id: body.session_id });
	return json({ ok: true });
}
