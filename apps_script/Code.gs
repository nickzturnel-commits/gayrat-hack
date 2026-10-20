/** Secure X Drive delivery. Set Script Properties RENDER_URL and SYNC_KEY, then run setup(). */
const SX = {
  PROP_URL: 'RENDER_URL',
  PROP_KEY: 'SYNC_KEY',
  CURSOR: 'SYNC_CURSOR',
  START_BUDGET_MS: 4 * 60 * 1000 + 45 * 1000,
  RETRY_PAUSE_MS: 12000
};

function setup() {
  const props = PropertiesService.getScriptProperties();
  const url = (props.getProperty(SX.PROP_URL) || '').trim();
  const key = (props.getProperty(SX.PROP_KEY) || '').trim();
  if (!url || !key) throw new Error('Добавьте RENDER_URL и SYNC_KEY в Script Properties, затем снова запустите setup().');
  if (!/^https:\/\//i.test(url)) throw new Error('RENDER_URL должен начинаться с https://.');
  // Validation only. The existing updateSub time trigger is intentionally preserved.
  Logger.log('Настройки проверены. Существующий триггер updateSub оставлен без изменений.');
}

// Render calls this deployed web app immediately after an admin change.
// Deploy once as a Web app (execute as you; access: anyone); SYNC_KEY is checked here.
function doPost(e) {
  let lock;
  try {
    const props = PropertiesService.getScriptProperties();
    const base = (props.getProperty(SX.PROP_URL) || '').trim().replace(/\/+$/, '');
    const key = (props.getProperty(SX.PROP_KEY) || '').trim();
    const data = JSON.parse((e && e.postData && e.postData.contents) || '{}');
    if (!base || !key || !constantTimeEquals_(String(data.sync_key || ''), key)) {
      return json_({ ok: false, error: 'Недействительные настройки или доступ.' });
    }
    if (data.action !== 'sync') return json_({ ok: false, error: 'Неизвестное действие.' });
    lock = LockService.getScriptLock();
    lock.waitLock(15000);

    const manifest = fetchManifest_(base, key, Array.isArray(data.user_ids) ? data.user_ids : null);
    if (!manifest || !Array.isArray(manifest.users) || !manifest.users.length) {
      return json_({ ok: false, error: 'Манифест пуст или недоступен.' });
    }
    const wanted = Array.isArray(data.user_ids) ? new Set(data.user_ids.map(String)) : null;
    const users = wanted ? manifest.users.filter(user => wanted.has(String(user.id))) : manifest.users;
    let processed = 0;
    let failed = 0;
    users.forEach(user => {
      try {
        processUser_(base, key, user);
        processed++;
      } catch (err) {
        failed++;
        const message = safeError_(err);
        Logger.log('Немедленная доставка не удалась для id=' + String(user && user.id) + ': ' + message);
        if (user && user.id != null) {
          try {
            const rememberedId = PropertiesService.getScriptProperties().getProperty('PENDING_FILE_' + user.id);
            report_(base, key, user.id, user.drive_file_id || rememberedId || null, false, message);
          } catch (reportErr) {
            Logger.log('Не удалось отправить отчёт для id=' + String(user.id) + '.');
          }
        }
      }
    });
    Logger.log('Немедленная доставка: обработано ' + processed + ', ошибок ' + failed + '.');
    return json_({ ok: true, processed: processed, failed: failed });
  } catch (err) {
    Logger.log('Немедленная доставка не выполнена (' + safeError_(err) + ').');
    return json_({ ok: false, error: 'Не удалось выполнить доставку.' });
  } finally {
    if (lock && lock.hasLock()) lock.releaseLock();
  }
}

function json_(value) {
  return ContentService.createTextOutput(JSON.stringify(value)).setMimeType(ContentService.MimeType.JSON);
}

function constantTimeEquals_(left, right) {
  let diff = left.length ^ right.length;
  const size = Math.max(left.length, right.length);
  for (let i = 0; i < size; i++) diff |= (left.charCodeAt(i) || 0) ^ (right.charCodeAt(i) || 0);
  return diff === 0;
}

function updateSub() {
  const lock = LockService.getScriptLock();
  try {
    lock.waitLock(15000);
  } catch (err) {
    Logger.log('Синхронизация уже выполняется; повтор будет при следующем запуске.');
    return;
  }
  try {
  const started = Date.now();
  const props = PropertiesService.getScriptProperties();
  const base = (props.getProperty(SX.PROP_URL) || '').trim().replace(/\/+$/, '');
  const key = (props.getProperty(SX.PROP_KEY) || '').trim();
  if (!base || !key) {
    Logger.log('Синхронизация пропущена: выполните setup() после задания Script Properties.');
    return;
  }

  const manifest = fetchManifest_(base, key);
  if (!manifest || !Array.isArray(manifest.users) || manifest.users.length === 0) {
    Logger.log('Манифест пустой или некорректный; файлы Drive не изменены.');
    return;
  }

  const users = manifest.users;
  let cursor = Math.max(0, parseInt(props.getProperty(SX.CURSOR) || '0', 10) || 0) % users.length;
  let processed = 0;
  let next = cursor;
  for (let offset = 0; offset < users.length; offset++) {
    if (Date.now() - started > SX.START_BUDGET_MS) break;
    const index = (cursor + offset) % users.length;
    const user = users[index];
    next = (index + 1) % users.length;
    try {
      processUser_(base, key, user);
    } catch (err) {
      const message = safeError_(err);
      Logger.log('Сбой пользователя id=' + String(user && user.id) + ': ' + message);
      if (user && user.id != null) {
        try {
          const rememberedId = PropertiesService.getScriptProperties().getProperty('PENDING_FILE_' + user.id);
          report_(base, key, user.id, user.drive_file_id || rememberedId || null, false, message);
        } catch (reportErr) {
          Logger.log('Не удалось отправить отчёт для id=' + String(user.id) + ': ' + safeError_(reportErr));
        }
      }
    }
    processed++;
  }
  if (processed >= users.length) {
    props.deleteProperty(SX.CURSOR);
  } else {
    props.setProperty(SX.CURSOR, String(next));
    Logger.log('Остановлено по бюджету времени; продолжение начнётся с элемента ' + next + '.');
  }
  Logger.log('Синхронизация завершена: обработано ' + processed + ' из ' + users.length + '.');
  } finally {
    lock.releaseLock();
  }
}

function fetchManifest_(base, key, userIds) {
  const suffix = userIds && userIds.length ? '?user_ids=' + encodeURIComponent(userIds.map(String).join(',')) : '';
  const url = base + '/api/sync/manifest' + suffix;
  let lastStatus = 0;
  for (let attempt = 1; attempt <= 2; attempt++) {
    try {
      const response = UrlFetchApp.fetch(url, {
        method: 'get',
        headers: { 'X-Sync-Key': key, 'Accept': 'application/json' },
        muteHttpExceptions: true,
        followRedirects: true
      });
      lastStatus = response.getResponseCode();
      const text = response.getContentText();
      if (lastStatus >= 200 && lastStatus < 300 && text) {
        const parsed = JSON.parse(text);
        if (parsed && Array.isArray(parsed.users)) return parsed;
      }
      Logger.log('Render manifest вернул HTTP ' + lastStatus + '.');
    } catch (err) {
      Logger.log('Ошибка запроса manifest (' + safeError_(err) + ').');
    }
    if (attempt < 2) Utilities.sleep(SX.RETRY_PAUSE_MS);
  }
  Logger.log('Manifest недоступен после повторной попытки (HTTP ' + lastStatus + '); Drive не изменён.');
  return null;
}

function processUser_(base, key, user) {
  if (!user || user.id == null || !user.slug) throw new Error('Некорректная запись пользователя в манифесте.');
  const props = PropertiesService.getScriptProperties();
  let fileId = user.drive_file_id || props.getProperty('PENDING_FILE_' + user.id) || null;
  if (user.status === 'pending_delete') {
    if (fileId) deleteDriveFilePermanently_(fileId);
    report_(base, key, user.id, fileId, true, null);
    props.deleteProperty('PENDING_FILE_' + user.id);
    Logger.log('Подписка пользователя id=' + user.id + ' удалена из Drive окончательно.');
    return;
  }
  if (typeof user.content !== 'string' || !user.content.trim()) throw new Error('Манифест не содержит готовую подписку; файл оставлен без изменений.');

  let file;
  const newFile = !user.drive_file_id;
  if (fileId) {
    file = DriveApp.getFileById(fileId);
  } else {
    file = DriveApp.createFile(String(user.name || user.slug), user.content, MimeType.PLAIN_TEXT);
    fileId = file.getId();
    // Preserve the new ID in case reporting to Render fails; avoid duplicate files on retry.
    props.setProperty('PENDING_FILE_' + user.id, fileId);
  }
  if (newFile) {
    file.setSharing(DriveApp.Access.ANYONE_WITH_LINK, DriveApp.Permission.VIEW);
  }
  if (file.getBlob().getDataAsString('UTF-8') !== user.content) {
    file.setContent(user.content);
  }
  report_(base, key, user.id, fileId, true, null);
  props.deleteProperty('PENDING_FILE_' + user.id);
  Logger.log('Пользователь id=' + user.id + ': файл синхронизирован.');
}

// The DriveApp File API only supports moving a file to Trash. Use Drive API v3
// DELETE so a manual admin deletion permanently removes the file. Treat 404 as
// success to make retries safe when Drive deletion succeeded but reporting did not.
function deleteDriveFilePermanently_(fileId) {
  const response = UrlFetchApp.fetch(
    'https://www.googleapis.com/drive/v3/files/' + encodeURIComponent(String(fileId)),
    {
      method: 'delete',
      headers: { Authorization: 'Bearer ' + ScriptApp.getOAuthToken() },
      muteHttpExceptions: true,
      followRedirects: true
    }
  );
  const code = response.getResponseCode();
  if ((code < 200 || code >= 300) && code !== 404) {
    throw new Error('Drive API не смог окончательно удалить файл (HTTP ' + code + ').');
  }
}

function report_(base, key, userId, fileId, ok, error) {
  const payload = { user_id: Number(userId), drive_file_id: fileId || null, ok: Boolean(ok) };
  if (!ok && error) payload.error = String(error).slice(0, 700);
  const response = UrlFetchApp.fetch(base + '/api/sync/report', {
    method: 'post',
    contentType: 'application/json',
    payload: JSON.stringify(payload),
    headers: { 'X-Sync-Key': key, 'Accept': 'application/json' },
    muteHttpExceptions: true,
    followRedirects: true
  });
  const code = response.getResponseCode();
  if (code < 200 || code >= 300) throw new Error('Отчёт синхронизации вернул HTTP ' + code + '.');
}

function safeError_(err) {
  // Do not log request URLs, credentials, subscription contents or server URIs.
  const message = err && err.message ? String(err.message) : 'неизвестная ошибка';
  return message.replace(/https?:\/\/\S+/gi, '[адрес скрыт]').replace(/(?:vless|vmess|trojan|ss|hysteria2?|tuic):\/\/\S+/gi, '[сервер скрыт]').slice(0, 700);
}
