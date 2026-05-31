// Focus Button Backend — Cloudflare Worker
// Endpoints:
//   POST /press               — Button reports a press
//   GET  /state               — Mac asks "should I be blocking?"
//   GET  /dispense-pending    — Dispenser asks "should I drop candy?"
//   POST /dispense-ack        — Dispenser confirms candy dropped
//   GET  /health              — Sanity check

interface Env {
	DB: D1Database;
}

interface ActiveSession {
	id: number;
	startedAt: number;
	endsAt: number;
	durationMinutes: number;
}

const SHARED_SECRET = '966c7278e3d618c677d8772e612ea6f79e6d575d53bac63229d3af670c02b28a';
const DEFAULT_DURATION_MINUTES = 60;
const DISPENSE_WINDOW_SECONDS = 30;

export default {
	async fetch(request: Request, env: Env): Promise<Response> {
		const url = new URL(request.url);
		const method = request.method;

		// Health endpoint is public (no auth) so you can verify the worker is up
		if (method === 'GET' && url.pathname === '/health') {
			return json({ ok: true });
		}

		// Every other endpoint requires the shared secret
		const auth = request.headers.get('Authorization');
		if (auth !== `Bearer ${SHARED_SECRET}`) {
			return json({ error: 'unauthorized' }, 401);
		}

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

// ===== Handlers =====

async function handlePress(request: Request, env: Env): Promise<Response> {
	const body = (await request.json().catch(() => ({}))) as {
		duration_minutes?: number;
	};
	const duration = body.duration_minutes || DEFAULT_DURATION_MINUTES;

	// Always log the raw press
	await logEvent(env, 'button_press', { duration_minutes: duration });

	// Check for an active session
	const activeSession = await getActiveSession(env);

	if (activeSession) {
		// Option C: press during active session does nothing else
		return json({
			ok: true,
			action: 'logged_only',
			session_unchanged: true,
			minutes_remaining: Math.ceil((activeSession.endsAt - now()) / 60),
		});
	}

	// No active session: start one
	await logEvent(env, 'session_started', { duration_minutes: duration });

	return json({
		ok: true,
		action: 'session_started',
		duration_minutes: duration,
		ends_at: now() + duration * 60,
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
	// Find the most recent session_started event
	const session = await env.DB.prepare(
		"SELECT id, timestamp FROM events WHERE type = 'session_started' ORDER BY timestamp DESC LIMIT 1",
	).first<{ id: number; timestamp: number }>();

	if (!session) {
		return json({ pending: false });
	}

	// Has it been acknowledged?
	const ack = await env.DB.prepare("SELECT id FROM events WHERE type = 'dispense_ack' AND timestamp >= ? LIMIT 1")
		.bind(session.timestamp)
		.first();

	if (ack) {
		return json({ pending: false });
	}

	// Is the session_started event recent enough to still dispense for?
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
