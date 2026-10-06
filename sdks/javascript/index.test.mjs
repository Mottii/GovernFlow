import {test} from 'node:test';
import assert from 'node:assert/strict';
import {Gateway, ActionBlocked} from './index.mjs';

test('blocked actions never execute, approved actions consume before execution', async () => {
  const calls = [];
  let decision = 'pending';
  const gateway = new Gateway('http://gateway', 'virtual', {fetchImpl: async (url, options) => {
    calls.push(url);
    assert.ok(!options.body?.includes('signing_secret'));
    return new Response(JSON.stringify(url.endsWith('/consume') ? {consumed: true} :
      {decision, token: decision === 'allow' ? 'opaque' : undefined}), {status: 200});
  }});
  let executed = false;
  await assert.rejects(() => gateway.execute('send', {}, 'Send', () => {executed = true;}, {waitTimeout: 0}), ActionBlocked);
  assert.equal(executed, false);
  decision = 'allow';
  assert.equal(await gateway.execute('send', {}, 'Send', () => {
    assert.ok(calls.at(-1).endsWith('/consume')); return 'done';
  }), 'done');
});
