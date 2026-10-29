/* Keep share actions mutually exclusive: QR must never route as copy-link. */
(function (root, factory) {
  const actions = factory();
  if (typeof module === 'object' && module.exports) module.exports = actions;
  else root.PanelAdminActions = actions;
})(globalThis, function () {
  'use strict';

  function resolveUserAction(button) {
    const action = button && button.dataset && button.dataset.action;
    if (action === 'qr') return { kind: 'qr', userId: button.dataset.userId };
    if (action === 'copy-link') {
      return { kind: 'copy-link', userId: button.dataset.userId, link: button.dataset.link || '' };
    }
    return null;
  }

  function formatDate(value) {
    if (value === null || value === undefined || value === '') return '—';

    let parsed;
    if (typeof value === 'number' || (typeof value === 'string' && value.trim() !== '' && Number.isFinite(Number(value)))) {
      const timestamp = Number(value);
      if (!Number.isFinite(timestamp) || timestamp === 0) return '—';
      // Python time.time() values are seconds; Date expects milliseconds.
      parsed = new Date(Math.abs(timestamp) < 1e12 ? timestamp * 1000 : timestamp);
    } else {
      parsed = new Date(value);
    }

    if (!Number.isFinite(parsed.getTime())) return '—';
    try {
      return parsed.toLocaleString('ru-RU', { dateStyle: 'medium', timeStyle: 'short' });
    } catch {
      return '—';
    }
  }

  return { resolveUserAction, formatDate };
});
