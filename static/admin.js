(() => {
  'use strict';

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  })[char]);
  let csrf = '';
  let page = 'dashboard';
  let toastTimer;
  let activeQrObjectUrl = '';

  function toast(message) {
    const node = $('#toast');
    node.textContent = message;
    node.classList.remove('hidden');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => node.classList.add('hidden'), 3600);
  }

  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    const method = (options.method || 'GET').toUpperCase();
    if (csrf && method !== 'GET' && method !== 'HEAD') headers.set('X-CSRF-Token', csrf);
    if (options.body && typeof options.body !== 'string') {
      headers.set('Content-Type', 'application/json');
      options.body = JSON.stringify(options.body);
    }
    const response = await fetch(path, { ...options, method, headers, credentials: 'same-origin' });
    const raw = await response.text();
    let data;
    try { data = raw ? JSON.parse(raw) : null; } catch { data = raw; }
    if (!response.ok) {
      throw new Error(typeof data === 'object' && data ? data.detail || 'Ошибка запроса' : data || 'Ошибка запроса');
    }
    return data;
  }

  function date(value) {
    if (!value) return '—';
    try { return new Date(value).toLocaleString('ru-RU', { dateStyle: 'medium', timeStyle: 'short' }); }
    catch { return String(value); }
  }

  function bytes(value) {
    let amount = Number(value) || 0;
    const units = ['Б', 'КБ', 'МБ', 'ГБ', 'ТБ'];
    let index = 0;
    while (amount >= 1000 && index < units.length - 1) { amount /= 1000; index++; }
    return `${Number(amount.toFixed(index ? 1 : 0)).toLocaleString('ru-RU')} ${units[index]}`;
  }

  const icons = {
    dashboard: '<path d="m3 10 9-7 9 7M5 9v11h14V9m-9 11v-6h4v6"/>',
    users: '<circle cx="9" cy="8" r="4"/><path d="M2 21v-2a7 7 0 0 1 14 0v2m1-17a4 4 0 0 1 0 8m1 3a6 6 0 0 1 4 5v1"/>',
    servers: '<rect x="3" y="4" width="18" height="6" rx="2"/><rect x="3" y="14" width="18" height="6" rx="2"/><path d="M7 7h.01M7 17h.01M11 7h6m-6 10h6"/>',
    settings: '<circle cx="12" cy="12" r="3"/><path d="m19 13 2 1-2 4-2-1a8 8 0 0 1-2 1l-.4 2h-4L10 18a8 8 0 0 1-2-1l-2 1-2-4 2-1a8 8 0 0 1 0-2l-2-1 2-4 2 1a8 8 0 0 1 2-1l.4-2h4L15 6a8 8 0 0 1 2 1l2-1 2 4-2 1a8 8 0 0 1 0 2Z"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    refresh: '<path d="M20 7v5h-5M4 17v-5h5"/><path d="M5.6 9a7 7 0 0 1 11.7-2L20 12M4 12l2.7 5a7 7 0 0 0 11.7-2"/>',
    copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3"/>',
    eye: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>',
    edit: '<path d="m4 16-.8 4.8L8 20l11-11-4-4L4 16Z"/><path d="m13.5 6.5 4 4"/>',
    trash: '<path d="M3 6h18M8 6V4h8v2m3 0-1 14H6L5 6m4 4v6m6-6v6"/>',
    link: '<path d="M10 13a5 5 0 0 0 7.1 0l3-3A5 5 0 0 0 13 2.9l-1.7 1.7"/><path d="M14 11a5 5 0 0 0-7.1 0l-3 3A5 5 0 0 0 11 21.1l1.7-1.7"/>',
    qr: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><path d="M14 14h3v3h-3zm5 0h2m-7 5v2m5-2h2v2h-2"/>',
    close: '<path d="m6 6 12 12M18 6 6 18"/>'
  };
  function svg(name, extra = '') {
    return `<svg class="icon-svg ${extra}" viewBox="0 0 24 24" aria-hidden="true">${icons[name] || ''}</svg>`;
  }
  function buttonIcon(name, label, attrs = '') {
    return `<button type="button" class="icon-button" aria-label="${esc(label)}" title="${esc(label)}" ${attrs}>${svg(name)}</button>`;
  }

  function setPage(next) {
    page = next;
    const navButtons = $$('#nav [data-page]');
    navButtons.forEach((button, index) => {
      const active = button.dataset.page === page;
      button.classList.toggle('active', active);
      if (active) button.setAttribute('aria-current', 'page');
      else button.removeAttribute('aria-current');
      if (active) $('#nav').dataset.activeIndex = String(index);
    });
    render();
  }

  function showLogin() {
    $('#loginView').classList.remove('hidden');
    $('#appView').classList.add('hidden');
    $('#logout').classList.add('hidden');
  }

  function showApp() {
    $('#loginView').classList.add('hidden');
    $('#appView').classList.remove('hidden');
    $('#logout').classList.remove('hidden');
    render();
  }

  $('#loginForm').addEventListener('submit', async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const error = $('#loginError');
    const submit = $('button[type="submit"]', form);
    submit.disabled = true;
    error.classList.add('hidden');
    try {
      const fields = new FormData(form);
      const result = await api('/api/admin/auth/login', { method: 'POST', body: {
        username: fields.get('username'), password: fields.get('password')
      }});
      csrf = result.csrf_token;
      form.reset();
      showApp();
    } catch (failure) {
      error.textContent = failure.message;
      error.classList.remove('hidden');
    } finally { submit.disabled = false; }
  });

  $('#logout').addEventListener('click', async () => {
    try { await api('/api/admin/auth/logout', { method: 'POST', body: {} }); } catch { /* session may have expired */ }
    csrf = '';
    showLogin();
  });

  $('#nav').addEventListener('click', event => {
    const button = event.target.closest('[data-page]');
    if (button) setPage(button.dataset.page);
  });
  $('#closeModal').addEventListener('click', () => $('#modal').close());
  $('#modal').addEventListener('close', releaseQrObjectUrl);
  $('#modal').addEventListener('click', event => {
    if (event.target === $('#modal')) $('#modal').close();
  });

  function pageHead(title, action = '') {
    return `<div class="page-head"><div><p class="eyebrow">SECURE X / ПАНЕЛЬ</p><h1>${title}</h1></div>${action}</div>`;
  }

  async function render() {
    const content = $('#content');
    content.setAttribute('aria-busy', 'true');
    content.innerHTML = '<div class="loading-card"><span class="spinner"></span><span>Загружаю данные</span></div>';
    try {
      if (page === 'dashboard') await dashboard(content);
      else if (page === 'users') await users(content);
      else if (page === 'servers') await servers(content);
      else await settings(content);
    } catch (failure) {
      content.innerHTML = `<div class="error-card"><div class="status-icon">!</div><div><h2>Не удалось загрузить раздел</h2><p>${esc(failure.message)}</p><button id="retryPage">Попробовать снова</button></div></div>`;
      $('#retryPage').addEventListener('click', render, { once: true });
    } finally { content.setAttribute('aria-busy', 'false'); }
  }

  async function dashboard(root) {
    const data = await api('/api/admin/dashboard');
    const traffic = data.traffic || {};
    const delivery = data.drive_delivery || {};
    const sources = data.sources || {};
    const sas = sources.sas || {};
    const primary = sources.primary || {};
    const banners = [
      !data.database.connected ? `<div class="alert-card">${esc(data.database.error || 'База данных недоступна')}</div>` : '',
      data.upstream.warning ? `<div class="alert-card">Апстрим: ${esc(data.upstream.warning)}. Используется последняя удачная копия, если она доступна.</div>` : '',
      !delivery.configured ? '<div class="alert-card">Для мгновенной отправки изменений задайте в Render DRIVE_SYNC_URL — адрес развёрнутого Apps Script Web App.</div>' : '',
      delivery.warning ? `<div class="alert-card">${esc(delivery.warning)}</div>` : ''
    ].filter(Boolean).join('');
    root.innerHTML = pageHead('Обзор', `<button id="retryDriveSync" class="button button-secondary">${svg('refresh')}<span>Обновить Drive</span></button>`)
      + banners
      + `<div class="stats-grid">
        <article class="stat-card"><div class="stat-icon">${svg('settings')}</div><p>База данных</p><strong class="${data.database.connected ? 'tone-good' : 'tone-bad'}">${data.database.connected ? 'Подключена' : 'Недоступна'}</strong><small>Supabase</small></article>
        <article class="stat-card"><div class="stat-icon">${svg('servers')}</div><p>Серверы апстрима</p><strong>${Number(data.upstream.server_count) || 0}</strong><small>Обновлено: ${esc(date(data.upstream.last_success_at))}</small></article>
        <article class="stat-card"><div class="stat-icon">${svg('users')}</div><p>Пользователи</p><strong>${data.users.active} <span class="stat-divider">/</span> ${data.users.total}</strong><small>активны · отключено ${data.users.disabled}</small></article>
        <article class="stat-card"><div class="stat-icon">${svg('refresh')}</div><p>Трафик аккаунта провайдера</p><strong>${traffic.total ? `${esc(bytes((traffic.upload || 0) + (traffic.download || 0)))} / ${esc(bytes(traffic.total))}` : 'Безлимит'}</strong><small>Загрузка ${esc(bytes(traffic.upload))} · скачивание ${esc(bytes(traffic.download))}</small></article>
        <article class="stat-card"><div class="stat-icon">${svg('link')}</div><p>Google Drive</p><strong class="${delivery.configured ? 'tone-good' : 'tone-warn'}">${delivery.configured ? 'Синхронизация включена' : 'Не настроено'}</strong><small>Последнее обновление: ${esc(date(delivery.last_success_at))}</small></article>
      </div>
      <section class="section-block source-health-block"><div class="section-heading"><div><p class="eyebrow">ПОДКЛЮЧЁННЫЕ ИСТОЧНИКИ</p><h2>Состав каталога</h2></div><span class="source-refresh-note">Обновление SAS каждые 5 минут</span></div>
        <div class="source-health-grid">
          <article class="source-health-card primary-health"><div class="health-card-head"><span class="health-source-mark">SX</span><span class="badge ${primary.configured ? 'badge-good' : 'badge-warn'}">${primary.configured ? 'Основной' : 'Не настроен'}</span></div><p>Secure X</p><strong>${Number(primary.server_count) || 0}<small> серверов</small></strong><div class="health-meter"><i style="width:${primary.configured ? '100%' : '0%'}"></i></div><small class="health-caption">Основная подписка проекта</small></article>
          <article class="source-health-card sas-health"><div class="health-card-head"><span class="health-source-mark">SAS</span><span class="badge ${sas.warning ? 'badge-warn' : sas.configured ? 'badge-good' : 'badge-muted'}">${sas.warning ? 'Есть предупреждение' : sas.configured ? 'Подключён' : 'Не настроен'}</span></div><p>Источник SAS</p><strong>${Number(sas.server_count) || 0}<small> серверов</small></strong><div class="health-meter"><i style="width:${sas.configured ? '100%' : '0%'}"></i></div><small class="health-caption">${sas.warning ? esc(sas.warning) : sas.last_success_at ? `Обновлён ${esc(date(Number(sas.last_success_at) * 1000))}` : 'Ожидает первого обновления'}</small></article>
        </div>
        <div class="dashboard-shortcuts"><button type="button" class="button button-secondary" data-dashboard-page="users">${svg('users')}<span>Подписки</span></button><button type="button" class="button button-secondary" data-dashboard-page="servers">${svg('servers')}<span>Управлять серверами</span></button><button type="button" class="button button-secondary" data-dashboard-page="settings">${svg('settings')}<span>Настройки источников</span></button></div>
      </section>`
      + (data.sync_errors?.length ? `<section class="section-block"><div class="section-heading"><div><p class="eyebrow">ТРЕБУЕТ ВНИМАНИЯ</p><h2>Ошибки синхронизации</h2></div></div><div class="card-list">${data.sync_errors.map(item => `<article class="surface-card"><h3>${esc(item.name)}</h3><p class="tone-bad">${esc(item.last_sync_error)}</p><small>${esc(date(item.last_synced_at))}</small></article>`).join('')}</div></section>` : '');
    root.querySelectorAll('[data-dashboard-page]').forEach(button => button.addEventListener('click', () => setPage(button.dataset.dashboardPage)));
  }

  function releaseQrObjectUrl() {
    if (!activeQrObjectUrl) return;
    URL.revokeObjectURL(activeQrObjectUrl);
    activeQrObjectUrl = '';
  }

  function openModal(title, content) {
    const modal = $('#modal');
    releaseQrObjectUrl();
    if (modal.open) modal.close();
    $('#modalTitle').textContent = title;
    $('#modalBody').innerHTML = content;
    modal.showModal();
  }

  function selectForCopy(field) {
    if (!field) return;
    try { field.focus({ preventScroll: true }); } catch { field.focus(); }
    field.select();
    if (typeof field.setSelectionRange === 'function') field.setSelectionRange(0, field.value.length);
  }

  function copyText(value, visibleField = null) {
    // Start Clipboard API while the original tap is active. Keep the legacy
    // synchronous fallback for browsers that do not expose it (or reject it).
    let clipboardWrite = null;
    if (window.isSecureContext && navigator.clipboard?.writeText) {
      try { clipboardWrite = Promise.resolve(navigator.clipboard.writeText(value)); }
      catch { /* Use the synchronous fallback below. */ }
    }

    let temporaryField = null;
    const field = visibleField || (temporaryField = document.createElement('textarea'));
    if (temporaryField) {
      temporaryField.value = value;
      temporaryField.setAttribute('readonly', '');
      temporaryField.setAttribute('aria-hidden', 'true');
      Object.assign(temporaryField.style, {
        position: 'fixed', top: '0', left: '0', width: '1px', height: '1px',
        padding: '0', border: '0', opacity: '0.01', fontSize: '16px',
        transform: 'scale(1)',
      });
      document.body.append(temporaryField);
    }
    selectForCopy(field);
    let copied = false;
    try { copied = document.execCommand('copy'); } catch { /* Clipboard command can be disabled by the browser. */ }
    if (temporaryField) temporaryField.remove();
    if (copied) {
      toast('Ссылка скопирована');
      if (clipboardWrite) clipboardWrite.catch(() => {});
      return true;
    }

    if (clipboardWrite) {
      return clipboardWrite.then(
        () => { toast('Ссылка скопирована'); return true; },
        () => {
          if (visibleField) selectForCopy(visibleField);
          toast(visibleField
            ? 'Ссылка выделена — удерживайте её и выберите «Копировать»'
            : 'Автокопирование запрещено — открыл ссылку для копирования вручную');
          return false;
        }
      );
    }
    if (visibleField) selectForCopy(visibleField);
    toast(visibleField
      ? 'Ссылка выделена — удерживайте её и выберите «Копировать»'
      : 'Автокопирование запрещено — открыл ссылку для копирования вручную');
    return false;
  }

  function driveLinkContent(link) {
    return `<div class="drive-share"><p class="field-hint">Ссылка подписки</p><label class="drive-link-field"><input data-drive-url readonly value="${esc(link)}" autocapitalize="off" autocomplete="off" spellcheck="false" aria-label="Ссылка подписки"></label><div class="drive-share-actions"><button class="button button-primary" type="button" data-copy-drive>${svg('copy')}<span>Скопировать ссылку</span></button></div></div>`;
  }

  async function showDriveShare(userId, readyLink = null) {
    let link = readyLink;
    if (!link) link = (await api(`/api/admin/users/${encodeURIComponent(userId)}/drive-link`)).link;
    if (!link) { toast('Файл ещё не создан в Drive'); return; }
    openModal('Ссылка Drive', driveLinkContent(link));
    const body = $('#modalBody');
    body.onclick = event => {
      const copy = event.target.closest('[data-copy-drive]');
      if (copy) {
        const input = $('[data-drive-url]', body);
        copyText(input.value, input);
      }
    };
  }

  function driveQrLoadingContent() {
    return '<div class="qr-only"><div class="drive-qr-frame qr-loading" aria-label="Загрузка QR-кода"><span class="spinner" aria-hidden="true"></span></div></div>';
  }

  async function showDriveQr(userId) {
    openModal('QR-код подписки', driveQrLoadingContent());
    const body = $('#modalBody');
    try {
      const response = await fetch(`/api/admin/users/${encodeURIComponent(userId)}/drive-qr.svg`, {
        method: 'GET', cache: 'no-store', credentials: 'same-origin',
        headers: { Accept: 'image/svg+xml' },
      });
      const svgText = await response.text();
      if (!response.ok) {
        let message = 'Не удалось загрузить QR-код';
        try { message = JSON.parse(svgText).detail || message; } catch { /* Use the safe generic message. */ }
        throw new Error(response.status === 401 ? 'Сессия завершилась — войдите в панель снова' : message);
      }
      if (!(response.headers.get('content-type') || '').toLowerCase().includes('image/svg+xml') || !/<svg\b/i.test(svgText)) {
        throw new Error('Сервер вернул некорректное изображение QR-кода');
      }

      const imageUrl = URL.createObjectURL(new Blob([svgText], { type: 'image/svg+xml' }));
      activeQrObjectUrl = imageUrl;
      const frame = document.createElement('div');
      frame.className = 'drive-qr-frame';
      const image = document.createElement('img');
      image.alt = 'QR-код ссылки подписки';
      image.width = 260;
      image.height = 260;
      image.onload = () => frame.classList.add('qr-ready');
      image.onerror = () => {
        frame.classList.add('qr-error');
        frame.textContent = 'Не удалось отобразить QR-код. Попробуйте ещё раз.';
      };
      frame.append(image);
      const qrOnly = document.createElement('div');
      qrOnly.className = 'qr-only';
      qrOnly.append(frame);
      body.replaceChildren(qrOnly);
      image.src = imageUrl;
    } catch (failure) {
      body.innerHTML = `<div class="qr-error-card" role="alert">${esc(failure.message || 'Не удалось загрузить QR-код')}</div>`;
    }
  }

  function userStatus(user) {
    const expired = user.expires_at && new Date(user.expires_at).getTime() <= Date.now();
    if (user.status === 'pending_delete') return ['Удаляется', 'badge-warn'];
    if (expired) return ['Срок истёк', 'badge-warn'];
    return user.enabled ? ['Включена', 'badge-good'] : ['Отключена', 'badge-warn'];
  }

  async function users(root) {
    const data = await api('/api/admin/users');
    const list = data.users || [];
    const activeCount = list.filter(user => userStatus(user)[0] === 'Включена').length;
    const sasCount = list.filter(user => user.include_sas !== false).length;
    const inactiveCount = list.length - activeCount;
    root.innerHTML = pageHead('Подписки', `<button id="addUser" class="button button-primary">${svg('plus')}<span>Создать подписку</span></button>`)
      + (list.length ? `<section class="collection-toolbar surface-card">
          <div class="collection-overview"><div><p class="eyebrow">ПРОФИЛИ GOOGLE DRIVE</p><strong>${list.length} <small>всего</small></strong></div><span class="overview-divider"></span><div><strong>${activeCount} <small>активны</small></strong></div><span class="overview-divider"></span><div><strong>${sasCount} <small>с SAS</small></strong></div></div>
          <div class="collection-tools"><label class="search-box"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="10.8" cy="10.8" r="6.8"/><path d="m16 16 5 5"/></svg><input id="userSearch" type="search" autocomplete="off" placeholder="Найти подписку по имени или заметке"></label>
            <div class="filter-pills" id="userFilters" aria-label="Фильтр подписок">
              <button type="button" class="filter-chip active" data-user-filter="all" aria-pressed="true">Все <span>${list.length}</span></button>
              <button type="button" class="filter-chip" data-user-filter="sas" aria-pressed="false">С SAS <span>${sasCount}</span></button>
              <button type="button" class="filter-chip" data-user-filter="primary" aria-pressed="false">Только Secure X <span>${list.length - sasCount}</span></button>
              <button type="button" class="filter-chip" data-user-filter="inactive" aria-pressed="false">Неактивные <span>${inactiveCount}</span></button>
            </div>
          </div>
        </section>
        <div class="result-caption"><span id="userResultCount">${list.length} подписок</span><span>Изменения отправляются в Google Drive</span></div>
        <div class="card-list" id="userList">${list.map(user => {
        const [status, tone] = userStatus(user);
        const state = status === 'Включена' ? 'active' : 'inactive';
        return `<article class="surface-card user-card" data-user-card data-user-name="${esc(`${user.name || ''} ${user.note || ''}`.toLowerCase())}" data-user-state="${state}" data-user-sas="${user.include_sas === false ? 'off' : 'on'}">
          <div class="user-card-head"><div class="avatar-mark">${esc((user.name || '?').trim().slice(0, 1).toUpperCase())}</div><div class="user-main"><h2>${esc(user.name)}</h2><p>${esc(user.note || 'Без заметки')}</p></div><span class="badge ${tone}">${status}</span><span class="badge ${user.drive_file_id ? 'badge-good' : 'badge-muted'}">${user.drive_file_id ? 'Drive готов' : 'Ожидает Drive'}</span><span class="badge ${user.include_sas === false ? 'badge-muted' : 'source-sas'}">${user.include_sas === false ? 'Только Secure X' : 'Secure X + SAS'}</span>${user.hide_traffic_usage && user.slug !== 'legacy' ? '<span class="badge badge-muted">Счётчик скрыт</span>' : ''}</div>
          <div class="user-meta"><span>Срок действия</span><strong>${esc(date(user.expires_at))}</strong></div>
          <div class="user-actions">
            <button class="button button-secondary" data-edit="${user.id}">${svg('edit')}<span>Изменить</span></button>
            <button class="button button-secondary" data-servers="${user.id}">${svg('servers')}<span>Серверы</span></button>
            <button class="button button-secondary" data-preview="${user.id}">${svg('eye')}<span>Предпросмотр</span></button>
            <button class="button button-secondary" data-action="copy-link" data-link="${esc(user.drive_link || '')}" data-user-id="${user.id}" ${user.drive_link ? '' : 'disabled'}>${svg('link')}<span>Ссылка Drive</span></button>
            <button class="button button-secondary user-qr-action" data-action="qr" data-user-id="${user.id}" ${user.drive_link ? '' : 'disabled'}>${svg('qr')}<span>QR-код</span></button>
            <button class="button button-danger" data-delete="${user.id}" ${user.status === 'pending_delete' ? 'disabled' : ''}>${svg('trash')}<span>Удалить</span></button>
          </div>
        </article>`;
      }).join('')}</div><div id="userFilterEmpty" class="filter-empty hidden"><div class="empty-orbit">${svg('users')}</div><h2>Ничего не найдено</h2><p>Измените запрос или выберите другой фильтр.</p></div>` : '<div class="empty-state"><div class="empty-orbit">'+svg('users')+'</div><h2>Пока нет подписок</h2><p>Создайте профиль, выберите серверы Secure X и SAS и получите ссылку Google Drive.</p><button class="button button-primary" id="addUserEmpty">'+svg('plus')+'<span>Создать подписку</span></button></div>');
    $('#addUser').addEventListener('click', async () => {
      try { await userForm(null); } catch (failure) { toast(failure.message); }
    });
    if ($('#addUserEmpty')) $('#addUserEmpty').addEventListener('click', async () => {
      try { await userForm(null); } catch (failure) { toast(failure.message); }
    });
    if ($('#userSearch')) {
      const applyFilters = () => {
        const query = ($('#userSearch').value || '').trim().toLocaleLowerCase('ru-RU');
        const selected = $('.filter-chip.active', $('#userFilters'))?.dataset.userFilter || 'all';
        let visible = 0;
        $$('[data-user-card]', $('#userList')).forEach(card => {
          const matchesQuery = !query || card.dataset.userName.includes(query);
          const matchesFilter = selected === 'all'
            || (selected === 'sas' && card.dataset.userSas === 'on')
            || (selected === 'primary' && card.dataset.userSas === 'off')
            || (selected === 'inactive' && card.dataset.userState === 'inactive');
          card.hidden = !(matchesQuery && matchesFilter);
          if (!card.hidden) visible++;
        });
        $('#userResultCount').textContent = `${visible} из ${list.length} подписок`;
        $('#userFilterEmpty').classList.toggle('hidden', visible > 0);
      };
      $('#userSearch').addEventListener('input', applyFilters);
      $('#userFilters').addEventListener('click', event => {
        const chip = event.target.closest('[data-user-filter]');
        if (!chip) return;
        $$('.filter-chip', $('#userFilters')).forEach(item => {
          const active = item === chip;
          item.classList.toggle('active', active);
          item.setAttribute('aria-pressed', String(active));
        });
        applyFilters();
      });
    }
    root.onclick = event => handleUserAction(event, list);
  }

  async function handleUserAction(event, list) {
    const button = event.target.closest('button');
    if (!button) return;
    const shareAction = window.PanelAdminActions.resolveUserAction(button);
    try {
      if (shareAction?.kind === 'qr') {
        await showDriveQr(shareAction.userId);
      } else if (shareAction?.kind === 'copy-link') {
        const copied = copyText(shareAction.link);
        Promise.resolve(copied).then(ok => {
          if (!ok) return showDriveShare(shareAction.userId, shareAction.link).catch(failure => toast(failure.message));
        }).catch(failure => toast(failure.message));
      } else if (button.dataset.edit) {
        const user = list.find(item => item.id === Number(button.dataset.edit));
        if (user) await userForm(user);
      } else if (button.dataset.servers) {
        await userServers(button.dataset.servers);
      } else if (button.dataset.preview) {
        const result = await api(`/api/admin/users/${button.dataset.preview}/preview`);
        openModal('Предпросмотр профиля', `<pre class="preview">${esc(result)}</pre>`);
      } else if (button.dataset.delete && confirm('Отключить подписку и убрать пользователя из панели? Ссылка Drive останется, но при обновлении покажет один нерабочий сервер. Уже загруженные в клиент серверы могут работать до обновления подписки.')) {
        button.disabled = true;
        const result = await api(`/api/admin/users/${button.dataset.delete}`, { method: 'DELETE', body: {} });
        toast(resultText(result, 'Подписка отключена, ссылка заменена заглушкой'));
        await render();
      }
    } catch (failure) {
      if (button.dataset.delete && button.isConnected) button.disabled = false;
      toast(failure.message);
    }
  }

  function resultText(result, label) {
    if (result?.drive_sync?.ok) return `${label} · Drive обновлён`;
    if (result?.drive_sync) return `${label} · ${result.drive_sync.message}`;
    return label;
  }

  async function userForm(user) {
    let defaultHideTraffic = false;
    let defaultIncludeSas = true;
    if (!user) {
      const data = await api('/api/admin/settings');
      defaultHideTraffic = data.settings?.hide_traffic_for_new_users === true;
      defaultIncludeSas = data.settings?.include_sas_for_new_users !== false;
    }
    const isLegacy = user?.slug === 'legacy' || user?.drive_file_id === '160lsZ2nDrSXTzA09r9jeGHnd6QyFylw9';
    const hideTraffic = isLegacy ? false : (user ? user.hide_traffic_usage === true : defaultHideTraffic);
    const includeSas = user ? user.include_sas !== false : defaultIncludeSas;
    const expiry = user?.expires_at ? new Date(user.expires_at) : null;
    const localExpiry = expiry ? new Date(expiry.getTime() - expiry.getTimezoneOffset() * 60000).toISOString().slice(0, 16) : '';
    openModal(user ? 'Настройки подписки' : 'Создание подписки', `<form id="userForm" class="subscription-form">
      <section class="form-section"><p class="eyebrow">01 / ПРОФИЛЬ</p><h3>Кому выдаём подписку?</h3>
        <div class="field-pair subscription-fields"><label>Имя пользователя<input name="name" required maxlength="120" value="${esc(user?.name || '')}" placeholder="Например, Иван"></label>
          <label>Внутренняя заметка<textarea name="note" maxlength="2000" rows="2" placeholder="Например, тариф или контакт">${esc(user?.note || '')}</textarea></label></div>
      </section>
      <section class="form-section source-picker-section"><p class="eyebrow">02 / СОСТАВ ПОДПИСКИ</p><h3>Выберите источники серверов</h3>
        <fieldset class="source-picker"><legend class="visually-hidden">Источники серверов</legend><div class="source-choice-grid">
          <label class="source-choice ${includeSas ? 'selected' : ''}"><input type="radio" name="include_sas" value="with-sas" ${includeSas ? 'checked' : ''}><span class="choice-indicator"></span><span class="choice-copy"><b>Secure X + SAS</b><small>Серверы основного проекта и SAS</small></span><span class="choice-count">2 источника</span></label>
          <label class="source-choice ${includeSas ? '' : 'selected'}"><input type="radio" name="include_sas" value="securex-only" ${includeSas ? '' : 'checked'}><span class="choice-indicator"></span><span class="choice-copy"><b>Только Secure X</b><small>Без серверов из SAS</small></span><span class="choice-count">1 источник</span></label>
        </div></fieldset>
        <p class="field-hint">LTE — Обход глушилок из SAS исключены автоматически. Изменение состава обновит профиль в Google Drive.</p>
      </section>
      <section class="form-section"><p class="eyebrow">03 / ДОСТУП</p><h3>Срок и состояние</h3>
        <div class="field-pair subscription-fields"><label>Срок подписки<input name="expires_at" type="datetime-local" value="${localExpiry}"><span class="field-hint">Пустое поле — без срока.</span></label>
          <label class="switch-row access-switch"><input name="enabled" type="checkbox" ${user?.enabled === false ? '' : 'checked'}><span class="switch"></span><span><b>Подписка включена</b><small>При отключении ссылка покажет заглушку.</small></span></label></div>
        ${isLegacy ? '<div class="setting-note"><b>Счётчик трафика всегда виден</b><span>Это основная подписка; Happ показывает общий расход аккаунта провайдера.</span></div>' : `<label class="switch-row"><input name="hide_traffic_usage" type="checkbox" ${hideTraffic ? 'checked' : ''}><span class="switch"></span><span><b>Скрыть счётчик трафика</b><small>Управляет отображением общих данных провайдера в Happ.</small></span></label>`}
      </section>
      <section class="form-section"><p class="eyebrow">04 / ВИД ПРОФИЛЯ</p><h3>Заголовок и сообщение</h3>
        <label>Заголовок профиля<input name="title_override" maxlength="200" value="${esc(user?.title_override || '')}" placeholder="Использовать название по умолчанию"></label>
        <label>Объявление<textarea name="announce_override" maxlength="4000" rows="3" placeholder="Оставьте пустым для общего объявления">${esc(user?.announce_override || '')}</textarea></label>
      </section>
      <div class="form-actions"><button class="button button-primary" type="submit">${user ? 'Сохранить изменения' : 'Создать и синхронизировать'}</button></div>
    </form>`);
    $('#userForm').addEventListener('change', event => {
      if (event.target.name !== 'include_sas') return;
      $$('.source-choice', $('#userForm')).forEach(choice => {
        choice.classList.toggle('selected', $('input[name="include_sas"]', choice).checked);
      });
    });
    $('#userForm').addEventListener('submit', async event => {
      event.preventDefault();
      const form = event.currentTarget;
      const submit = $('button[type="submit"]', form);
      if (form.dataset.busy === '1') return;
      form.dataset.busy = '1'; submit.disabled = true;
      submit.innerHTML = `<span class="spinner"></span><span>${user ? 'Сохраняю изменения…' : 'Создаю профиль…'}</span>`;
      const values = new FormData(form);
      const expiryValue = values.get('expires_at');
      try {
        const body = {
          name: values.get('name'), note: values.get('note'), enabled: values.has('enabled'),
          expires_at: expiryValue ? new Date(expiryValue).toISOString() : null,
          title_override: values.get('title_override') || null,
          announce_override: values.get('announce_override') || null,
          // The legacy account always keeps the provider counter visible.
          hide_traffic_usage: isLegacy ? false : values.has('hide_traffic_usage'),
          include_sas: values.get('include_sas') === 'with-sas',
          traffic_limit_bytes: user?.traffic_limit_bytes ?? null
        };
        const result = await api(user ? `/api/admin/users/${user.id}` : '/api/admin/users', {
          method: user ? 'PUT' : 'POST', body
        });
        $('#modal').close();
        toast(resultText(result, 'Пользователь сохранён'));
        await render();
      } catch (failure) {
        form.dataset.busy = '0'; submit.disabled = false;
        submit.innerHTML = `<span>${user ? 'Сохранить изменения' : 'Создать и синхронизировать'}</span>`;
        toast(failure.message);
      }
    });
  }

  async function userServers(userId) {
    const result = await api(`/api/admin/users/${userId}/servers`);
    const rows = result.servers || [];
    openModal('Серверы пользователя', rows.length ? `<div class="server-override-intro"><p>Личные настройки действуют только для этой подписки. Пустое имя наследует глобальное.</p></div><div class="card-list">${rows.map(server => `<article class="surface-card compact-card">
      <div class="list-title"><div><h3>${esc(server.user_name || server.global_name || server.original_name)}</h3><small>${server.source === 'sas' ? '<span class="source-inline">SAS</span> · ' : '<span class="source-inline source-securex">Secure X</span> · '}${esc(server.original_name)}${server.present_upstream ? '' : ' · отсутствует в апстриме'}</small></div></div>
      <div class="rename-row"><label>Имя для этой подписки<input data-us-name="${esc(server.id)}" value="${esc(server.user_name || '')}" placeholder="Использовать глобальное имя"></label><button type="button" class="button button-quiet" data-us-reset="${esc(server.id)}" ${server.user_name ? '' : 'disabled'}>Сбросить имя</button></div>
      <span class="field-hint">Пустое значение использует глобальное имя сервера.</span>
      <label class="switch-row"><input type="checkbox" data-us-hidden="${esc(server.id)}" ${server.user_hidden === true ? 'checked' : ''}><span class="switch"></span><span><b>Скрыть у этого пользователя</b></span></label>
      <button class="button button-secondary" data-us-save="${esc(server.id)}">Сохранить и обновить Drive</button>
    </article>`).join('')}</div>` : '<div class="empty-state"><h2>Список серверов пуст</h2><p>Обновите сервера в соответствующем разделе.</p></div>');
    $('#modalBody').onclick = async event => {
      const resetButton = event.target.closest('[data-us-reset]');
      const saveButton = event.target.closest('[data-us-save]');
      const button = resetButton || saveButton;
      if (!button) return;
      button.disabled = true;
      const serverId = button.dataset.usReset || button.dataset.usSave;
      const server = rows.find(item => String(item.id) === serverId);
      try {
        if (resetButton) $(`[data-us-name="${CSS.escape(serverId)}"]`).value = '';
        const saved = await api(`/api/admin/users/${userId}/servers/${serverId}`, { method: 'PUT', body: {
          hidden: $(`[data-us-hidden="${CSS.escape(serverId)}"]`).checked,
          custom_name: $(`[data-us-name="${CSS.escape(serverId)}"]`).value || null
        }});
        if (server) {
          server.user_name = $(`[data-us-name="${CSS.escape(serverId)}"]`).value || null;
          const card = button.closest('.compact-card');
          $('h3', card).textContent = server.user_name || server.global_name || server.original_name;
        }
        toast(resultText(saved, resetButton ? 'Личное имя сброшено' : 'Настройки сервера сохранены'));
      } catch (failure) { toast(failure.message); }
      finally { button.disabled = resetButton ? !server?.user_name : false; }
    };
    $('#modalBody').oninput = event => {
      const field = event.target.closest('[data-us-name]');
      if (!field) return;
      const reset = $(`[data-us-reset="${CSS.escape(field.dataset.usName)}"]`);
      if (reset) reset.disabled = !field.value.trim();
    };
  }

  async function servers(root) {
    const result = await api('/api/admin/servers');
    const list = result.servers || [];
    const sasCount = list.filter(server => server.source === 'sas').length;
    const primaryCount = list.length - sasCount;
    const hiddenCount = list.filter(server => server.hidden).length;
    root.innerHTML = pageHead('Серверы', `<button id="refreshServers" class="button button-secondary">${svg('refresh')}<span>Обновить источники</span></button>`)
      + (list.length ? `<section class="server-overview surface-card"><div><p class="eyebrow">КАТАЛОГ СЕРВЕРОВ</p><strong>${list.length} <small>в профиле источников</small></strong><span>Названия сохраняются отдельно для Secure X и SAS.</span></div><div class="source-counts"><span class="source-count primary-count"><i></i> Secure X <b>${primaryCount}</b></span><span class="source-count sas-count"><i></i> SAS <b>${sasCount}</b></span><span class="source-count hidden-count"><i></i> Скрыты <b data-hidden-count>${hiddenCount}</b></span></div></section>
        <section class="server-toolbar surface-card">
          <label class="search-box"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="10.8" cy="10.8" r="6.8"/><path d="m16 16 5 5"/></svg><input id="serverSearch" type="search" autocomplete="off" placeholder="Поиск по имени сервера"></label>
          <div class="filter-pills" id="serverFilters" aria-label="Фильтр серверов">
            <button type="button" class="filter-chip active" data-server-filter="all" aria-pressed="true">Все <span>${list.length}</span></button>
            <button type="button" class="filter-chip" data-server-filter="primary" aria-pressed="false">Secure X <span>${primaryCount}</span></button>
            <button type="button" class="filter-chip" data-server-filter="sas" aria-pressed="false">SAS <span>${sasCount}</span></button>
            <button type="button" class="filter-chip" data-server-filter="visible" aria-pressed="false">Показываются <span data-visible-server-count>${list.length - hiddenCount}</span></button>
            <button type="button" class="filter-chip" data-server-filter="hidden" aria-pressed="false">Скрыты <span data-hidden-server-count>${hiddenCount}</span></button>
          </div>
          <div class="bulk-server-bar"><label class="select-visible"><input id="selectVisibleServers" type="checkbox"><span class="check-ui"></span><b>Выбрать видимые</b></label><span id="selectedServerCount" class="selected-count">Выбрано: 0</span><div class="bulk-actions"><button type="button" class="button button-quiet" data-server-bulk="false" disabled>Показать</button><button type="button" class="button button-quiet" data-server-bulk="true" disabled>Скрыть</button></div></div>
        </section>
        <div class="result-caption"><span id="serverResultCount">${list.length} серверов</span><span>Массовые действия применяются к выбранным серверам</span></div>
        <div class="card-list" id="serverList">${list.map(server => `<article class="surface-card server-card" data-server-card data-server-source="${server.source === 'sas' ? 'sas' : 'primary'}" data-server-hidden="${server.hidden ? 'yes' : 'no'}" data-server-name="${esc(`${server.custom_name || ''} ${server.original_name || ''} ${server.id}`.toLocaleLowerCase('ru-RU'))}">
          <div class="server-select-row"><label class="server-select"><input type="checkbox" data-select-server="${esc(server.id)}"><span class="check-ui"></span><span>Выбрать</span></label><span class="server-id">ID · ${esc(server.id)}</span></div>
          <div class="server-card-head"><div class="server-mark">${svg('servers')}</div><div class="user-main"><h2>${esc(server.custom_name || server.original_name)}</h2><p>Исходное имя: ${esc(server.original_name)}</p></div><span class="badge ${server.source === 'sas' ? 'source-sas' : 'source-primary'}">${server.source === 'sas' ? 'SAS' : 'Secure X'}</span>${server.present_upstream ? '<span class="badge badge-good">Доступен</span>' : '<span class="badge badge-warn">Нет в источнике</span>'}</div>
          <div class="server-fields"><label>Глобальное имя<input data-name="${esc(server.id)}" value="${esc(server.custom_name || '')}" placeholder="Исходное имя"></label><label>Порядок в подписке<input data-order="${esc(server.id)}" type="number" value="${server.sort_order}" min="0"></label></div>
          <label class="switch-row server-visibility"><input data-hidden="${esc(server.id)}" type="checkbox" ${server.hidden ? 'checked' : ''}><span class="switch"></span><span><b>Скрыть этот сервер у всех</b><small>Личное исключение пользователя можно настроить в его подписке.</small></span></label>
          <div class="server-card-actions"><button type="button" class="button button-quiet" data-reset-server-name="${esc(server.id)}" ${server.custom_name ? '' : 'disabled'}>Сбросить имя</button><button type="button" class="button button-secondary" data-save-server="${esc(server.id)}">Сохранить и обновить Drive</button></div>
        </article>`).join('')}</div><div id="serverFilterEmpty" class="filter-empty hidden"><div class="empty-orbit">${svg('servers')}</div><h2>Серверы не найдены</h2><p>Поменяйте поисковый запрос или фильтр.</p></div>` : '<div class="empty-state"><div class="empty-orbit">'+svg('servers')+'</div><h2>Каталог пока пуст</h2><p>Обновите источники, чтобы загрузить серверы Secure X и SAS.</p></div>');
    $('#refreshServers').addEventListener('click', async event => {
      const button = event.currentTarget;
      button.disabled = true;
      button.innerHTML = '<span class="spinner"></span><span>Обновляю…</span>';
      try {
        const refreshed = await api('/api/admin/servers/refresh', { method: 'POST', body: {} });
        toast(resultText(refreshed, `Серверов: ${refreshed.count}`));
        await render();
      } catch (failure) { button.disabled = false; button.innerHTML = `${svg('refresh')}<span>Обновить источники</span>`; toast(failure.message); }
    });
    if (!list.length) return;
    const cards = $$('[data-server-card]', $('#serverList'));
    const selectedCount = () => cards.filter(card => $('[data-select-server]', card).checked).length;
    const updateSelection = () => {
      const selected = selectedCount();
      $('#selectedServerCount').textContent = `Выбрано: ${selected}`;
      $$('[data-server-bulk]').forEach(button => { button.disabled = selected === 0; });
      const visible = cards.filter(card => !card.hidden);
      const selectedVisible = visible.filter(card => $('[data-select-server]', card).checked).length;
      $('#selectVisibleServers').checked = visible.length > 0 && selectedVisible === visible.length;
      $('#selectVisibleServers').indeterminate = selectedVisible > 0 && selectedVisible < visible.length;
    };
    const applyServerFilters = () => {
      const query = ($('#serverSearch').value || '').trim().toLocaleLowerCase('ru-RU');
      const filter = $('.filter-chip.active', $('#serverFilters'))?.dataset.serverFilter || 'all';
      let visible = 0;
      cards.forEach(card => {
        const sourceMatch = filter === 'all' || filter === 'visible' || filter === 'hidden'
          || card.dataset.serverSource === filter;
        const visibilityMatch = filter === 'all' || filter === 'primary' || filter === 'sas'
          || (filter === 'visible' && card.dataset.serverHidden === 'no')
          || (filter === 'hidden' && card.dataset.serverHidden === 'yes');
        const match = sourceMatch && visibilityMatch && (!query || card.dataset.serverName.includes(query));
        card.hidden = !match;
        if (match) visible++;
      });
      $('#serverResultCount').textContent = `${visible} из ${list.length} серверов`;
      $('#serverFilterEmpty').classList.toggle('hidden', visible > 0);
      updateSelection();
    };
    $('#serverSearch').addEventListener('input', applyServerFilters);
    $('#serverFilters').addEventListener('click', event => {
      const chip = event.target.closest('[data-server-filter]');
      if (!chip) return;
      $$('.filter-chip', $('#serverFilters')).forEach(item => {
        const active = item === chip;
        item.classList.toggle('active', active);
        item.setAttribute('aria-pressed', String(active));
      });
      applyServerFilters();
    });
    $('#selectVisibleServers').addEventListener('change', event => {
      cards.filter(card => !card.hidden).forEach(card => { $('[data-select-server]', card).checked = event.target.checked; });
      updateSelection();
    });
    $('#serverList').addEventListener('change', event => {
      const checkbox = event.target.closest('[data-select-server]');
      if (checkbox) updateSelection();
    });
    $('#serverList').addEventListener('input', event => {
      const nameInput = event.target.closest('[data-name]');
      if (!nameInput) return;
      const reset = $(`[data-reset-server-name="${CSS.escape(nameInput.dataset.name)}"]`, root);
      if (reset) reset.disabled = !nameInput.value.trim();
    });
    root.onclick = async event => {
      const bulkButton = event.target.closest('[data-server-bulk]');
      if (bulkButton) {
        const ids = cards.filter(card => $('[data-select-server]', card).checked).map(card => $('[data-select-server]', card).dataset.selectServer);
        if (!ids.length) return;
        bulkButton.disabled = true;
        try {
          const hidden = bulkButton.dataset.serverBulk === 'true';
          const saved = await api('/api/admin/servers/bulk', { method: 'POST', body: { server_ids: ids, hidden } });
          toast(resultText(saved, hidden ? `Скрыто серверов: ${ids.length}` : `Показано серверов: ${ids.length}`));
          await render();
        } catch (failure) { bulkButton.disabled = false; toast(failure.message); }
        return;
      }
      const resetButton = event.target.closest('[data-reset-server-name]');
      const saveButton = event.target.closest('[data-save-server]');
      const button = resetButton || saveButton;
      if (!button) return;
      const id = button.dataset.resetServerName || button.dataset.saveServer;
      const nameInput = $(`[data-name="${CSS.escape(id)}"]`, root);
      if (resetButton) nameInput.value = '';
      button.disabled = true;
      const server = list.find(item => String(item.id) === id);
      try {
        const saved = await api(`/api/admin/servers/${encodeURIComponent(id)}`, { method: 'PUT', body: {
          custom_name: nameInput.value || null,
          sort_order: Number($(`[data-order="${CSS.escape(id)}"]`, root).value),
          hidden: $(`[data-hidden="${CSS.escape(id)}"]`, root).checked
        }});
        if (server) {
          server.custom_name = nameInput.value || null;
          server.hidden = $(`[data-hidden="${CSS.escape(id)}"]`, root).checked;
          server.sort_order = Number($(`[data-order="${CSS.escape(id)}"]`, root).value);
          const card = button.closest('[data-server-card]');
          $('h2', card).textContent = server.custom_name || server.original_name;
          card.dataset.serverHidden = server.hidden ? 'yes' : 'no';
          card.dataset.serverName = `${server.custom_name || ''} ${server.original_name || ''} ${server.id}`.toLocaleLowerCase('ru-RU');
          const currentlyHidden = list.filter(item => item.hidden).length;
          $('[data-hidden-count]', root).textContent = String(currentlyHidden);
          $('[data-hidden-server-count]', root).textContent = String(currentlyHidden);
          $('[data-visible-server-count]', root).textContent = String(list.length - currentlyHidden);
        }
        toast(resultText(saved, resetButton ? 'Исходное имя восстановлено' : 'Настройки сервера сохранены'));
        if (resetButton) $('[data-save-server]', button.closest('[data-server-card]')).focus();
        applyServerFilters();
      } catch (failure) { toast(failure.message); }
      finally { button.disabled = resetButton ? !server?.custom_name : false; }
    };
  }

  async function settings(root) {
    const data = await api('/api/admin/settings');
    const settings = data.settings || {};
    const sas = data.sas || {};
    const textarea = (name, label, hint = '') => `<label>${label}<textarea name="${name}" rows="${name === 'days' ? 5 : 7}">${esc((settings[name] || []).join('\n'))}</textarea>${hint ? `<span class="field-hint">${hint}</span>` : ''}</label>`;
    root.innerHTML = pageHead('Настройки') + `<form id="settingsForm" class="surface-card settings-form">
      <div class="form-section"><p class="eyebrow">ПРОФИЛЬ</p><h2>Внешний вид подписки</h2>
        <label>Название по умолчанию<input name="title" maxlength="200" value="${esc(settings.title)}"></label>
        <div class="field-pair"><label>Ссылка поддержки<input name="support_url" value="${esc(settings.support_url)}"></label><label>Страница профиля<input name="web_page_url" value="${esc(settings.web_page_url)}"></label></div>
        <div class="field-pair"><label>Интервал обновления (часы)<input name="update_interval" type="number" min="1" max="168" value="${settings.update_interval}"></label><label>Часовой пояс<input name="timezone" value="${esc(settings.timezone)}"></label></div>
      </div>
      <div class="form-section"><p class="eyebrow">ОБЪЯВЛЕНИЯ</p><h2>Сообщения пользователям</h2>
        <label class="switch-row"><input name="announce_enabled" type="checkbox" ${settings.announce_enabled ? 'checked' : ''}><span class="switch"></span><span><b>Объявления включены</b><small>Показываются в профиле подписки.</small></span></label>
        <label>Частота обновления объявления (минуты)<input name="announce_refresh_minutes" type="number" min="5" max="1440" value="${settings.announce_refresh_minutes || 5}"><span class="field-hint">День/приветствие, цитата и подпись меняются с этим интервалом; погода запрашивается при каждой синхронизации. Happ получает новый текст после записи в Drive. Для 10+ пользователей рекомендуем 60 минут, чтобы беречь квоту Apps Script. Минимум — 5 минут.</span></label>
      </div>
      <div class="form-section"><p class="eyebrow">НОВЫЕ ПОДПИСКИ</p><h2>Счётчик трафика в клиенте</h2>
        <label class="switch-row"><input name="hide_traffic_for_new_users" type="checkbox" ${settings.hide_traffic_for_new_users ? 'checked' : ''}><span class="switch"></span><span><b>Скрывать счётчик по умолчанию</b><small>Применяется только к новым пользователям. Переключатель можно изменить отдельно в каждой подписке. Основная подписка всегда показывает общий трафик аккаунта.</small></span></label>
        <span class="field-hint">Если счётчик включён, VPN-клиент увидит общий трафик провайдера. Это не отдельный лимит или учёт расхода конкретного пользователя.</span>
      </div>
      <div class="form-section sas-settings-section"><p class="eyebrow">ИСТОЧНИК SAS</p><h2>Серверы из SAS</h2>
        <article class="sas-status-card surface-card">
          <div class="sas-status-head"><span class="sas-status-mark">SAS</span><div><b>${sas.configured ? 'Источник настроен' : 'Источник не настроен'}</b><small>${sas.configured ? `Сейчас в источнике ${Number(sas.server_count) || 0} серверов. Вся группа «LTE — Обход глушилок» исключена.` : 'Добавьте SAS_SUBSCRIPTION_URL в переменные среды Secure X на Render.'}</small></div><span class="badge ${sas.configured ? 'badge-good' : 'badge-warn'}">${sas.configured ? 'Render' : 'Отключён'}</span></div>
          <div class="sas-status-meta"><span>Последнее успешное получение</span><strong>${esc(sas.last_success_at ? date(Number(sas.last_success_at) * 1000) : 'Ещё не загружено')}</strong></div>
          ${sas.warning ? `<p class="tone-warn sas-warning">${esc(sas.warning)}</p>` : ''}
          <p class="field-hint">Адрес источника хранится в переменной окружения Render. В этой панели управляются включение SAS в подписки, видимость серверов и их имена.</p>
        </article>
        <label class="switch-row"><input name="include_sas_for_new_users" type="checkbox" ${settings.include_sas_for_new_users !== false ? 'checked' : ''}><span class="switch"></span><span><b>Добавлять SAS в новые подписки по умолчанию</b><small>При создании каждой подписки это значение можно поменять отдельно. На уже существующие профили настройка не влияет.</small></span></label>
      </div>
      <div class="form-section"><p class="eyebrow">ТЕКСТЫ</p><h2>Дни, цитаты и подписи</h2>
        ${textarea('days', 'Дни недели — по одному на строку')}
        ${textarea('quotes', 'Цитаты — по одной на строку')}
        ${textarea('footers', 'Нижние строки; {t} заменяется текущим временем')}
      </div>
      <div class="form-actions"><button class="button button-primary" type="submit">Сохранить настройки</button></div>
    </form>`;
    $('#settingsForm').addEventListener('submit', async event => {
      event.preventDefault();
      const form = event.currentTarget;
      const submit = $('button[type="submit"]', form);
      if (form.dataset.busy === '1') return;
      form.dataset.busy = '1'; submit.disabled = true;
      submit.innerHTML = '<span class="spinner"></span><span>Сохраняю настройки…</span>';
      const fields = new FormData(form);
      const values = {
        title: fields.get('title'), support_url: fields.get('support_url'), web_page_url: fields.get('web_page_url'),
        update_interval: Number(fields.get('update_interval')), timezone: fields.get('timezone'),
        announce_enabled: fields.has('announce_enabled'), announce_refresh_minutes: Number(fields.get('announce_refresh_minutes')),
        hide_traffic_for_new_users: fields.has('hide_traffic_for_new_users'),
        include_sas_for_new_users: fields.has('include_sas_for_new_users'),
        days: fields.get('days').split('\n').map(line => line.trim()).filter(Boolean),
        quotes: fields.get('quotes').split('\n').map(line => line.trim()).filter(Boolean),
        footers: fields.get('footers').split('\n').map(line => line.trim()).filter(Boolean)
      };
      try {
        const saved = await api('/api/admin/settings', { method: 'PUT', body: { values } });
        toast(resultText(saved, 'Настройки сохранены'));
        form.dataset.busy = '0'; submit.disabled = false;
        submit.innerHTML = '<span>Сохранить настройки</span>';
      } catch (failure) {
        form.dataset.busy = '0'; submit.disabled = false;
        submit.innerHTML = '<span>Сохранить настройки</span>';
        toast(failure.message);
      }
    });
  }

  document.addEventListener('click', async event => {
    const button = event.target.closest('#retryDriveSync');
    if (!button) return;
    button.disabled = true;
    button.innerHTML = '<span class="spinner"></span><span>Отправляю…</span>';
    try {
      const result = await api('/api/admin/drive/sync', { method: 'POST', body: {} });
      toast(result.message || 'Синхронизация завершена');
      await render();
    } catch (failure) { toast(failure.message); }
    finally { button.disabled = false; }
  });

  async function init() {
    try {
      const result = await api('/api/admin/auth/me');
      csrf = result.csrf_token;
      showApp();
    } catch { showLogin(); }
  }
  init();
})();
