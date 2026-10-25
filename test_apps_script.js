const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync('apps_script/Code.gs', 'utf8');

function loadScript({ apiWriteStatus = 200, reportOk = true, currentContent = 'old content' } = {}) {
  const calls = [];
  const properties = new Map();
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
        const status = url.startsWith('https://www.googleapis.com/upload/drive/v3/files/')
          ? apiWriteStatus : 200;
        return {
          getResponseCode: () => status,
          getContentText: () => JSON.stringify({ ok: reportOk })
        };
      }
    },
    DriveApp: {
      getFileById: () => file,
      createFile: () => { throw new Error('Unexpected Drive file creation'); }
    },
    Utilities: {
      Charset: { UTF_8: 'UTF-8' },
      base64Encode: (value, charset) => Buffer.from(value, charset).toString('base64')
    },
    MimeType: { PLAIN_TEXT: 'text/plain' },
    Logger: { log: () => {} }
  };
  vm.runInNewContext(source, sandbox);
  return { sandbox, calls, properties, file };
}

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
