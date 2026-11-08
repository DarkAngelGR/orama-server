import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { randomBytes } from 'node:crypto';

test('built demo enforces authentication and serves packaged assets', async () => {
  const password = randomBytes(24).toString('hex');
  const child = spawn(process.execPath, ['dist/server.js'], { env: { ...process.env, ORAMA_DEMO_PASSWORD: password }, stdio: ['ignore', 'pipe', 'pipe'] });
  try {
    await new Promise((resolve, reject) => {
      child.stdout.once('data', resolve);
      child.once('exit', code => reject(new Error(`Server exited: ${code}`)));
      child.stderr.on('data', data => reject(new Error(String(data))));
    });
    const base = 'http://127.0.0.1:3000';
    const get = path => fetch(base + path, { redirect: 'manual' });
    const page = await get('/signin');
    assert.equal(page.status, 200);
    const html = await page.text();
    const csrf = html.match(/name="csrf_token" value="([^"]+)"/)[1];
    const post = (path, data, cookie = '') => fetch(base + path, {
      method: 'POST', redirect: 'manual', headers: { 'Content-Type': 'application/x-www-form-urlencoded', Cookie: cookie },
      body: new URLSearchParams({ csrf_token: csrf, ...data }),
    });
    assert.equal((await get('/static/orama.css')).status, 200);
    assert.equal((await get('/?token=default-session-token')).status, 302);
    for (const data of [{ quick_login: '1' }, { password: 'admin' }, { password: 'orama' }, { password: 'orama-stack-2026' }]) {
      assert.equal((await post('/signin', data)).status, 401);
    }
    assert.equal((await post('/setup/password', { password })).status, 404);
    assert.equal((await post('/signin', { password, csrf_token: 'invalid' })).status, 403);
    const login = await post('/signin?next=//example.com/', { password });
    assert.equal(login.status, 302);
    assert.equal(login.headers.get('location'), '/');
    const cookie = login.headers.get('set-cookie');
    assert.match(cookie, /HttpOnly/i);
    assert.match(cookie, /SameSite=Strict/i);
    const home = await fetch(base, { headers: { Cookie: cookie.split(';')[0] } });
    assert.equal(home.status, 200);
    assert.ok(!(await home.text()).includes(cookie.split(';')[0].split('=')[1]));
    assert.equal((await post('/setup/password', { password }, cookie.split(';')[0])).status, 404);
    const change = await post('/settings/password', {
      current_password: password, new_password: password + 'changed', repeat_password: password + 'changed',
    }, cookie.split(';')[0]);
    assert.equal(change.status, 302);
    assert.equal((await post('/signin', { password })).status, 401);
    const renewed = await post('/signin?next=/settings', { password: password + 'changed' });
    assert.equal(renewed.headers.get('location'), '/settings');
    const signout = await post('/signout', {}, cookie.split(';')[0]);
    assert.equal(signout.status, 302);
    assert.equal((await fetch(base, { redirect: 'manual', headers: { Cookie: cookie.split(';')[0] } })).status, 302);
  } finally { child.kill(); }
});
