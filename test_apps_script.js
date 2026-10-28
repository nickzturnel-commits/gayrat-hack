const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync('apps_script/Code.gs', 'utf8');

function loadScript({
  apiWriteStatus = 200,
  reportOk = true,
  currentContent = 'old content',
  manifestBody = JSON.stringify({ users: [] }),
  weatherStatus = 200,
  weatherBody = JSON.stringify({
    utc_offset_seconds: 18000,
    timezone: 'Asia/Ashgabat',
    current: { time: '2026-10-27T19:00', interval: 900, temperature_2m: 16.2 }
  })
} = {}) {
  const calls = [];
  const properties = new Map();
  const cache = new Map();
  const logs = [];
  const file = {
    getBlob: () => ({ getDataAsString: () => currentContent }),
    setContent: content => { currentContent = content; file.contentWritten = content; },
    setSharing: () => { throw new Error('Existing Drive file should not be reshared'); }
  };
  const sandbox = {
    console,
    encodeURIComponent,
    JSON,
    Date,
    Math,
    String,
    Number,
    Boolean,
    Set,
    PropertiesService: {
      getScriptProperties: () => ({
        getProperty: key => properties.get(key) || null,
        setProperty: (key, value) => properties.set(key, value),
        deleteProperty: key => properties.delete(key)
      })
    },
    ScriptApp: { getOAuthToken: () => 'mock-oauth-token' },
    UrlFetchApp: {
      fetch: (url, options) => {
        calls.push({ url, options });
        const isWeather = url.startsWith('https://api.open-meteo.com/');
        const isDriveWrite = url.startsWith('https://www.googleapis.com/upload/drive/v3/files/');
        const isReport = url.endsWith('/api/sync/report');
        const status = isWeather ? weatherStatus : (isDriveWrite ? apiWriteStatus : 200);
        return {
          getResponseCode: () => status,
          getContentText: () => isWeather ? weatherBody
            : (isDriveWrite || isReport ? JSON.stringify({ ok: reportOk }) : manifestBody)
        };
      }
    },
    CacheService: {
      getScriptCache: () => ({
        get: key => cache.get(key) || null,
        put: (key, value) => cache.set(key, value)
      })
    },
    DriveApp: {
      getFileById: () => file,
      createFile: () => { throw new Error('Unexpected Drive file creation'); }
    },
    Utilities: {
      Charset: { UTF_8: 'UTF-8' },
      base64Encode: (value, charset) => Buffer.from(value, charset).toString('base64'),
      base64EncodeWebSafe: value => Buffer.from(value, 'utf8').toString('base64url')
    },
    MimeType: { PLAIN_TEXT: 'text/plain' },
    Logger: { log: message => logs.push(String(message)) }
  };
  vm.runInNewContext(source, sandbox);
  return { sandbox, calls, properties, file, logs, cache };
}

test('manifest fetch gets weather in Apps Script and forwards the temperature snapshot to Render', () => {
  const { sandbox, calls, cache } = loadScript({ manifestBody: JSON.stringify({ users: [{ id: 1 }] }) });
  sandbox.fetchManifest_('https://render.example', 'sync-key');
  sandbox.fetchManifest_('https://render.example', 'sync-key');

  const weatherCalls = calls.filter(call => call.url.startsWith('https://api.open-meteo.com/'));
  const manifestCalls = calls.filter(call => call.url.startsWith('https://render.example/api/sync/manifest'));
  assert.equal(weatherCalls.length, 1); // ScriptCache prevents repeated requests during a sync burst.
  assert.equal(weatherCalls[0].url,
    'https://api.open-meteo.com/v1/forecast?latitude=37.9601&longitude=58.3261&current=temperature_2m&timezone=Asia%2FAshgabat');
  assert.equal(cache.has('ASHGABAT_WEATHER_V1'), true);
  assert.equal(manifestCalls.length, 2);

  const encoded = manifestCalls[0].options.headers['X-Weather-Data'];
  const payload = JSON.parse(Buffer.from(encoded, 'base64url').toString('utf8'));
  assert.equal(payload.current.temperature_2m, 16.2);
  assert.equal(payload.current.time, '2026-10-27T19:00');
  assert.equal(manifestCalls[0].options.headers['X-Sync-Key'], 'sync-key');
});

test('weather provider errors are logged by status and forwarded as an unavailable marker', () => {
  const { sandbox, calls, logs } = loadScript({
    weatherStatus: 429,
    weatherBody: JSON.stringify({ error: true, reason: 'Rate limit exceeded' }),
    manifestBody: JSON.stringify({ users: [] })
  });
  sandbox.fetchManifest_('https://render.example', 'sync-key');

  const manifestCall = calls.find(call => call.url.startsWith('https://render.example/api/sync/manifest'));
  const payload = JSON.parse(Buffer.from(manifestCall.options.headers['X-Weather-Data'], 'base64url').toString('utf8'));
  assert.deepEqual(payload, { weather_unavailable: true });
  assert.ok(logs.some(message => message.includes('Open-Meteo weather HTTP 429')));
  assert.ok(logs.some(message => message.includes('Rate limit exceeded')));
});

test('pending_delete keeps the Drive URL and replaces the profile with one disabled node', () => {
  const { sandbox, calls, properties, file } = loadScript();
  sandbox.processUser_('https://render.example', 'sync-key', {
    id: 7, slug: 'manual', status: 'pending_delete', drive_file_id: 'file-id'
  });

  assert.equal(calls.length, 2);
  assert.equal(calls[0].options.method, 'patch');
  assert.equal(calls[0].url, 'https://www.googleapis.com/upload/drive/v3/files/file-id?uploadType=media');
  assert.match(calls[0].options.payload, /#profile-title: base64:/);
  assert.match(calls[0].options.payload, /#announce: base64:/);
  assert.match(calls[0].options.payload, /vless:\/\/.*@127\.0\.0\.1:1\?encryption=none&type=tcp#/);
  assert.ok(calls[0].options.payload.endsWith(`#${encodeURIComponent('Подписка отключена')}\n`));
  assert.equal((calls[0].options.payload.match(/(?:vless|vmess|trojan|ss|hysteria2?|tuic):\/\//g) || []).length, 1);
  assert.equal(calls[0].options.headers.Authorization, 'Bearer mock-oauth-token');
  assert.equal(calls[1].url, 'https://render.example/api/sync/report');
  assert.equal(file.contentWritten, undefined); // Drive media upload is done through UrlFetchApp.
  assert.equal(properties.has('PENDING_FILE_7'), false);
});

test('repeated revocation skips a Drive write when the tombstone is already present', () => {
  const { sandbox, calls, file } = loadScript();
  // The current Drive file already contains the exact stable revocation profile.
  const content = sandbox.revokedProfile_();
  file.getBlob = () => ({ getDataAsString: () => content });
  sandbox.processUser_('https://render.example', 'sync-key', {
    id: 12, slug: 'manual', status: 'pending_delete', drive_file_id: 'file-id'
  });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, 'https://render.example/api/sync/report');
});

test('already-missing Drive file is retry-safe during revocation', () => {
  const { sandbox, calls } = loadScript({ apiWriteStatus: 404 });
  assert.doesNotThrow(() => sandbox.processUser_('https://render.example', 'sync-key', {
    id: 8, slug: 'manual', status: 'pending_delete', drive_file_id: 'missing-id'
  }));
  assert.equal(calls.length, 2);
});

test('failed Drive revocation does not report success or clear its retry marker', () => {
  const { sandbox, calls, properties } = loadScript({ apiWriteStatus: 403 });
  properties.set('PENDING_FILE_10', 'file-10');
  assert.throws(() => sandbox.processUser_('https://render.example', 'sync-key', {
    id: 10, slug: 'manual', status: 'pending_delete', drive_file_id: 'file-10'
  }), /HTTP 403/);
  assert.equal(calls.length, 1);
  assert.equal(properties.get('PENDING_FILE_10'), 'file-10');
});

test('failed Render acknowledgement keeps pending deletion retryable', () => {
  const { sandbox, calls, properties } = loadScript({ reportOk: false });
  properties.set('PENDING_FILE_11', 'file-11');
  assert.throws(() => sandbox.processUser_('https://render.example', 'sync-key', {
    id: 11, slug: 'manual', status: 'pending_delete', drive_file_id: 'file-11'
  }), /не подтвердил отчёт/);
  assert.equal(calls.length, 2);
  assert.equal(properties.get('PENDING_FILE_11'), 'file-11');
});

test('expired profiles remain active records and update their placeholder file', () => {
  const { sandbox, calls, file } = loadScript({ currentContent: 'old profile' });
  const placeholder = 'vless://127.0.0.1:1#Время подписки закончилось';
  sandbox.processUser_('https://render.example', 'sync-key', {
    id: 9, slug: 'expired', status: 'active', enabled: true,
    drive_file_id: 'expired-file', content: placeholder
  });

  assert.equal(file.contentWritten, placeholder);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, 'https://render.example/api/sync/report');
});

test('unchanged profiles do not rewrite their Google Drive file', () => {
  const content = '#profile-title: Secure X\nvless://same@server.example:443#Main\n';
  const { sandbox, calls, file } = loadScript({ currentContent: content });
  sandbox.processUser_('https://render.example', 'sync-key', {
    id: 14, slug: 'same-profile', status: 'active', drive_file_id: 'same-file', content
  });

  assert.equal(file.contentWritten, undefined);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, 'https://render.example/api/sync/report');
});
