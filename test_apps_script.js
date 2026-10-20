const assert = require('node:assert/strict');
const fs = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync('apps_script/Code.gs', 'utf8');

function loadScript({ apiDeleteStatus = 204, currentContent = 'old content' } = {}) {
  const calls = [];
  const properties = new Map();
  const file = {
    getBlob: () => ({ getDataAsString: () => currentContent }),
    setContent: content => { file.contentWritten = content; },
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
        const status = url.startsWith('https://www.googleapis.com/drive/v3/files/')
          ? apiDeleteStatus : 200;
        return { getResponseCode: () => status };
      }
    },
    DriveApp: {
      getFileById: () => file,
      createFile: () => { throw new Error('Unexpected Drive file creation'); }
    },
    MimeType: { PLAIN_TEXT: 'text/plain' },
    Logger: { log: () => {} }
  };
  vm.runInNewContext(source, sandbox);
  return { sandbox, calls, properties, file };
}

test('manual pending_delete uses Drive API hard delete and reports success', () => {
  const { sandbox, calls, properties } = loadScript({ apiDeleteStatus: 204 });
  sandbox.processUser_('https://render.example', 'sync-key', {
    id: 7, slug: 'manual', status: 'pending_delete', drive_file_id: 'file-id'
  });

  assert.equal(calls.length, 2);
  assert.equal(calls[0].options.method, 'delete');
  assert.equal(calls[0].url, 'https://www.googleapis.com/drive/v3/files/file-id');
  assert.equal(calls[0].options.headers.Authorization, 'Bearer mock-oauth-token');
  assert.equal(calls[1].url, 'https://render.example/api/sync/report');
  assert.equal(properties.has('PENDING_FILE_7'), false);
});

test('already-missing manually deleted Drive file is retry-safe', () => {
  const { sandbox, calls } = loadScript({ apiDeleteStatus: 404 });
  assert.doesNotThrow(() => sandbox.processUser_('https://render.example', 'sync-key', {
    id: 8, slug: 'manual', status: 'pending_delete', drive_file_id: 'missing-id'
  }));
  assert.equal(calls.length, 2);
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
