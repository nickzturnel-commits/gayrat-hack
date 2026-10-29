'use strict';

const assert = require('node:assert/strict');
const { test } = require('node:test');
const { resolveUserAction, formatDate } = require('./static/admin-actions.js');

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

test('formats Unix timestamps in seconds as calendar dates', () => {
  const seconds = Date.UTC(2026, 9, 29, 8, 7, 0) / 1000;
  const expected = new Date(seconds * 1000).toLocaleString('ru-RU', { dateStyle: 'medium', timeStyle: 'short' });
  assert.equal(formatDate(seconds), expected);
  assert.doesNotMatch(formatDate(seconds), /1970/);
});

test('keeps millisecond and ISO timestamps intact', () => {
  const milliseconds = Date.UTC(2026, 9, 29, 8, 7, 0);
  const expected = new Date(milliseconds).toLocaleString('ru-RU', { dateStyle: 'medium', timeStyle: 'short' });
  assert.equal(formatDate(milliseconds), expected);
  assert.equal(formatDate(new Date(milliseconds).toISOString()), expected);
});

test('renders missing and invalid timestamps as an em dash', () => {
  for (const value of [null, undefined, '', 0, 'not-a-date']) assert.equal(formatDate(value), '—');
});
