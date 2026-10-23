'use strict';

const assert = require('node:assert/strict');
const { test } = require('node:test');
const { resolveUserAction } = require('./static/admin-actions.js');

test('QR button routes only to QR and never reads a subscription link', () => {
  const button = { dataset: { action: 'qr', userId: '42', link: 'must-not-copy' } };
  assert.deepEqual(resolveUserAction(button), { kind: 'qr', userId: '42' });
});

test('subscription link button routes only to copy action', () => {
  const button = { dataset: { action: 'copy-link', userId: '42', link: 'https://example.test/sub' } };
  assert.deepEqual(resolveUserAction(button), {
    kind: 'copy-link', userId: '42', link: 'https://example.test/sub',
  });
});

test('unrelated user controls do not enter share handler', () => {
  assert.equal(resolveUserAction({ dataset: { edit: '42' } }), null);
});
