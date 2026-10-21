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

  return { resolveUserAction };
});
