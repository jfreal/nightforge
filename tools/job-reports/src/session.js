// Signing in to the hosted Routines page. There is one person, so the password is ADMIN_TOKEN, and
// a session is a cookie holding an expiry time and an HMAC of it keyed by that token. Rotating
// ADMIN_TOKEN signs everyone out. Nothing is stored server-side.

const COOKIE = "nf_session";
export const SESSION_DAYS = 30;
const enc = new TextEncoder();

async function key(secret) {
  return crypto.subtle.importKey("raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign", "verify"]);
}

const b64 = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const unb64 = (s) => Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/")), (c) => c.charCodeAt(0));

export async function makeSession(secret, now = Date.now()) {
  const exp = String(Math.floor(now / 1000) + SESSION_DAYS * 86400);
  const sig = await crypto.subtle.sign("HMAC", await key(secret), enc.encode("session:" + exp));
  return `${exp}.${b64(sig)}`;
}

/** True when the value is a session we signed and it hasn't expired. crypto.subtle.verify
 *  compares in constant time, so a wrong signature tells an attacker nothing. */
export async function validSession(secret, value, now = Date.now()) {
  const m = /^(\d{9,12})\.([A-Za-z0-9_-]{20,})$/.exec(value || "");
  if (!m || !secret) return false;
  if (Number(m[1]) * 1000 < now) return false;
  let sig;
  try { sig = unb64(m[2]); } catch { return false; }
  return crypto.subtle.verify("HMAC", await key(secret), sig, enc.encode("session:" + m[1]));
}

export function sessionFrom(request) {
  for (const part of (request.headers.get("Cookie") || "").split(";")) {
    const [k, ...v] = part.trim().split("=");
    if (k === COOKIE) return v.join("=");
  }
  return null;
}

export function setCookie(value, secure = true) {
  return `${COOKIE}=${value}; Path=/; HttpOnly; SameSite=Strict; Max-Age=${SESSION_DAYS * 86400}${secure ? "; Secure" : ""}`;
}

export function clearCookie(secure = true) {
  return `${COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0${secure ? "; Secure" : ""}`;
}

export function loginPage(error = "") {
  const msg = error ? `<p class="err" role="alert">${error}</p>` : "";
  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sign in · Routines</title><link rel="icon" href="/logo.png"><link rel="stylesheet" href="/style.css">
<style>.box{max-width:520px;margin:12vh auto;background:var(--card);border:2px solid var(--line);border-radius:16px;padding:28px 32px}
.box h1{font-size:32px;color:var(--ink)}.box input{width:100%;font:inherit;font-size:18px;padding:12px 14px;border:2px solid var(--btn-line);border-radius:10px;margin:10px 0 18px}
.box .err{background:#FFF4D6;border-radius:8px;padding:8px 12px;font-weight:700}</style></head>
<body><form class="box" method="post" action="/login">
<h1>Routines</h1>
<p>Paste this site's admin token to sign in. You stay signed in on this browser for ${SESSION_DAYS} days.</p>${msg}
<label for="t">Admin token</label><input id="t" name="token" type="password" autocomplete="current-password" required autofocus>
<button class="btn" type="submit">Sign in</button>
</form></body></html>`;
}
