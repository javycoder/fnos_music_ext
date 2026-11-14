const test = require('node:test');
const assert = require('node:assert');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const { createService, mapQualitySlots, mapSongDetail, cleanLyric, QUALITY_WHITELIST } = require('../service');

function makeRequest(app, method, urlPath, headers = {}, body = null) {
  return new Promise((resolve, reject) => {
    const server = http.createServer(app);
    server.listen(0, '127.0.0.1', () => {
      const port = server.address().port;
      const req = http.request({
        host: '127.0.0.1',
        port,
        path: urlPath,
        method,
        headers: {
          ...headers,
          ...(body ? { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) } : {}),
        },
      }, (res) => {
        const chunks = [];
        res.on('data', chunk => chunks.push(chunk));
        res.on('end', () => {
          const rawBuffer = Buffer.concat(chunks);
          const text = rawBuffer.toString('utf-8');
          let json = null;
          try {
            json = JSON.parse(text);
          } catch {}
          server.close(() => {
            resolve({
              status: res.statusCode,
              headers: res.headers,
              body: json,
              text,
              buffer: rawBuffer,
            });
          });
        });
      });
      req.on('error', (err) => {
        server.close(() => reject(err));
      });
      if (body) req.write(body);
      req.end();
    });
  });
}

test('Node.js runtime version satisfies requirement >= 22.13.0', () => {
  const [major, minor] = process.versions.node.split('.').map(Number);
  assert.ok(major > 22 || (major === 22 && minor >= 13), `Current node version ${process.version} must be >= 22.13.0`);
});

test('Upstream SDK module exports all required functions', () => {
  const sdk = require('@neteasecloudmusicapienhanced/api');
  const requiredFns = [
    'cloudsearch', 'search', 'song_detail', 'song_url_v1', 'lyric_new', 'lyric',
    'artists', 'album', 'playlist_detail', 'playlist_catlist', 'playlist_highquality_tags',
    'login_status', 'login_qr_key', 'login_qr_create', 'login_qr_check', 'user_playlist',
    'recommend_songs', 'toplist', 'personalized', 'top_playlist', 'top_playlist_highquality',
    'recommend_resource',
  ];
  for (const fn of requiredFns) {
    assert.strictEqual(typeof sdk[fn], 'function', `SDK must export ${fn}`);
  }
});

test('Quality slots and lyric cleaning functions', () => {
  const rawSong = {
    id: 101,
    name: 'Song A',
    ar: [{ name: 'Artist A' }],
    al: { name: 'Album A', picUrl: 'http://pic.jpg' },
    dt: 240000,
    fee: 1,
    l: { size: 1000, br: 128000 },
    h: { size: 2000, br: 320000 },
    sq: { size: 5000, br: 999000 },
  };
  const mapped = mapSongDetail(rawSong, { maxbr: 999000 });
  assert.strictEqual(mapped.song_id, 101);
  assert.strictEqual(mapped.name, 'Song A');
  assert.strictEqual(mapped.artist, 'Artist A');
  assert.strictEqual(mapped.album_name, 'Album A');
  assert.strictEqual(mapped.duration_ms, 240000);
  assert.strictEqual(mapped.maxbr, 999000);
  assert.strictEqual(mapped.has_sq, true);
  assert.strictEqual(mapped.has_hr, false);
  assert.strictEqual(mapped.qualities.standard.file_size, 1000);
  assert.strictEqual(mapped.qualities.exhigh.file_size, 2000);
  assert.strictEqual(mapped.qualities.lossless.file_size, 5000);
  assert.strictEqual(mapped.qualities.hires, undefined);

  const rawLrc = '{"t":0,"c":[{"tx":"作词: "}]}\n[00:10.00]Line 1\n{"t":2000,"c":[{"tx":"作曲: "}]}\n[00:20.00]Line 2';
  const cleaned = cleanLyric(rawLrc);
  assert.strictEqual(cleaned, '[00:10.00]Line 1\n[00:20.00]Line 2');
});

test('GET /healthz returns netease provider and upstream version offline', async () => {
  const mockSdk = {};
  const service = createService({ sdk: mockSdk });
  const res = await makeRequest(service.app, 'GET', '/healthz');
  assert.strictEqual(res.status, 200);
  assert.strictEqual(res.body.status, 'ok');
  assert.strictEqual(res.body.provider, 'netease');
  assert.strictEqual(res.body.upstream, '@neteasecloudmusicapienhanced/api');
  assert.strictEqual(res.body.version, '4.41.1');
});

test('GET /api/v1/search validation and mapping', async () => {
  const mockSdk = {
    cloudsearch: async ({ keywords, type, limit, offset }) => {
      assert.strictEqual(keywords, 'Beyond');
      assert.strictEqual(type, 1);
      return {
        body: {
          result: {
            songs: [
              { id: 101, name: '海阔天空', ar: [{ name: 'Beyond' }], al: { name: '海阔天空' }, dt: 320000, fee: 0 },
              { id: 102, name: '光辉岁月', ar: [{ name: 'Beyond' }], al: { name: '光辉岁月' }, dt: 300000, fee: 1 },
            ],
          },
        },
      };
    },
    song_url_v1: async ({ id }) => {
      assert.strictEqual(id, '101,102');
      return {
        body: {
          data: [
            { id: 101, url: 'http://cdn/101.mp3', code: 200, fee: 0 },
            { id: 102, url: '', code: 404, fee: 1 }, // unplayable
          ],
        },
      };
    },
    login_status: async () => ({ body: { data: { profile: null } } }),
  };

  const service = createService({ sdk: mockSdk });
  // Empty keyword
  const emptyRes = await makeRequest(service.app, 'GET', '/api/v1/search?keyword=');
  assert.strictEqual(emptyRes.status, 400);

  // Invalid type
  const invalidTypeRes = await makeRequest(service.app, 'GET', '/api/v1/search?keyword=test&type=invalid');
  assert.strictEqual(invalidTypeRes.status, 400);

  // Valid song search: 102 filtered out as unplayable
  const songRes = await makeRequest(service.app, 'GET', '/api/v1/search?keyword=Beyond&type=song');
  assert.strictEqual(songRes.status, 200);
  assert.strictEqual(songRes.body.ok, true);
  assert.strictEqual(songRes.body.data.length, 1);
  assert.strictEqual(songRes.body.data[0].song_id, 101);
  assert.strictEqual(songRes.body.data[0].artist, 'Beyond');
});

test('GET /api/v1/songs/detail batches and preserves requested order', async () => {
  const mockSdk = {
    song_detail: async ({ ids }) => {
      const idList = ids.split(',').map(Number);
      return {
        body: {
          songs: idList.map(id => ({
            id,
            name: `Song ${id}`,
            ar: [{ name: `Artist ${id}` }],
            al: { name: `Album ${id}`, picUrl: `http://pic/${id}.jpg` },
            dt: id * 1000,
            sq: { size: 10000, br: 999000 },
          })),
          privileges: idList.map(id => ({ id, maxbr: 999000 })),
        },
      };
    },
  };

  const service = createService({ sdk: mockSdk });
  const res = await makeRequest(service.app, 'GET', '/api/v1/songs/detail?ids=50,10,30');
  assert.strictEqual(res.status, 200);
  assert.strictEqual(res.body.ok, true);
  assert.deepStrictEqual(res.body.data.map(x => x.song_id), [50, 10, 30]);
  assert.strictEqual(res.body.data[0].album_pic_url, 'http://pic/50.jpg');
  assert.strictEqual(res.body.data[0].has_sq, true);
  assert.strictEqual(res.body.data[0].maxbr, 999000);

  // Validation errors
  const emptyParam = await makeRequest(service.app, 'GET', '/api/v1/songs/detail');
  assert.strictEqual(emptyParam.status, 422);
  const invalidId = await makeRequest(service.app, 'GET', '/api/v1/songs/detail?ids=10,abc');
  assert.strictEqual(invalidId.status, 422);
});

test('GET /api/v1/song/:song_id/url caching and quality validation', async () => {
  let callCount = 0;
  const mockSdk = {
    song_url_v1: async ({ id, level }) => {
      callCount += 1;
      return {
        body: {
          data: [
            { id, url: `http://cdn/${id}-${level}.flac`, code: 200, expi: 3600, level },
          ],
        },
      };
    },
  };

  const service = createService({ sdk: mockSdk });
  // Invalid quality
  const badQuality = await makeRequest(service.app, 'GET', '/api/v1/song/123/url?quality=ultra');
  assert.strictEqual(badQuality.status, 400);

  // First call (resolves and caches)
  const res1 = await makeRequest(service.app, 'GET', '/api/v1/song/123/url?quality=lossless');
  assert.strictEqual(res1.status, 200);
  assert.strictEqual(res1.body.data.code, 200);
  assert.strictEqual(res1.body.data.expiry_source, 'policy');
  assert.strictEqual(callCount, 1);

  // Second call (cache hit)
  const res2 = await makeRequest(service.app, 'GET', '/api/v1/song/123/url?quality=lossless');
  assert.strictEqual(res2.status, 200);
  assert.strictEqual(callCount, 1);

  // Fresh call (bypasses cache read)
  const resFresh = await makeRequest(service.app, 'GET', '/api/v1/song/123/url?quality=lossless&fresh=1');
  assert.strictEqual(resFresh.status, 200);
  assert.strictEqual(callCount, 2);
});

test('playback failures are handled, not cached, and do not terminate the service', async () => {
  let callCount = 0;
  const service = createService({
    sdk: {
      song_url_v1: async ({ id }) => {
        callCount += 1;
        if (callCount === 1) throw new Error('upstream offline');
        if (callCount === 2) return { body: { code: 500 } };
        return { body: { code: 200, data: [{ id, code: 200, url: 'https://cdn.test/song.flac' }] } };
      },
    },
  });

  const failed = await makeRequest(service.app, 'GET', '/api/v1/song/123/url');
  assert.strictEqual(failed.status, 502);
  await new Promise(resolve => setImmediate(resolve));
  const malformed = await makeRequest(service.app, 'GET', '/api/v1/song/123/url');
  assert.strictEqual(malformed.status, 502);
  await new Promise(resolve => setImmediate(resolve));
  const retry = await makeRequest(service.app, 'GET', '/api/v1/song/123/url');
  assert.strictEqual(retry.status, 200);
  assert.strictEqual(retry.body.data.url, 'https://cdn.test/song.flac');
  assert.strictEqual(callCount, 3);
});

test('batch playback probe follows the installed SDK query contract', async () => {
  const upstream = require('@neteasecloudmusicapienhanced/api/module/song_url_v1');
  let sentIds;
  const service = createService({
    sdk: {
      song_url_v1: query => upstream(query, async (url, data) => {
        sentIds = data.ids;
        return { body: { data: [{ id: 101, code: 200, url: 'https://cdn.test/101.mp3', fee: 0 }] } };
      }),
      login_status: async () => ({ body: { data: { profile: null } } }),
    },
  });
  const playable = await service.filterPlayableSongIds([101, 102]);
  assert.strictEqual(sentIds, '[101,102]');
  assert.deepStrictEqual([...playable], [101]);
});

test('GET /api/v1/song/:song_id/lyric extracts lyric and tlyric filtering JSON metadata', async () => {
  const mockSdk = {
    lyric_new: async ({ id }) => {
      return {
        body: {
          lrc: { lyric: '{"t":0,"c":[{"tx":"作词"}]}\n[00:01.00]First lyric\n[00:05.00]Second lyric' },
          tlyric: { lyric: '[00:01.00]第一句歌词\n[00:05.00]第二句歌词' },
        },
      };
    },
  };

  const service = createService({ sdk: mockSdk });
  const res = await makeRequest(service.app, 'GET', '/api/v1/song/888/lyric');
  assert.strictEqual(res.status, 200);
  assert.strictEqual(res.body.ok, true);
  assert.strictEqual(res.body.data.lyric, '[00:01.00]First lyric\n[00:05.00]Second lyric');
  assert.strictEqual(res.body.data.tlyric, '[00:01.00]第一句歌词\n[00:05.00]第二句歌词');
});

test('GET /api/v1/public-playlists/capabilities and categories', async () => {
  const mockSdk = {
    login_status: async () => ({ body: { data: { profile: null } } }),
    playlist_catlist: async () => ({
      body: {
        categories: { '0': '语种', '1': '风格' },
        sub: [
          { name: '华语', category: 0 },
          { name: '欧美', category: 0 },
          { name: '流行', category: 1 },
        ],
      },
    }),
    playlist_highquality_tags: async () => ({
      body: { tags: [{ name: '华语' }, { name: '摇滚' }] },
    }),
  };

  const service = createService({ sdk: mockSdk });
  const capsRes = await makeRequest(service.app, 'GET', '/api/v1/public-playlists/capabilities');
  assert.strictEqual(capsRes.status, 200);
  assert.strictEqual(capsRes.body.ok, true);
  assert.strictEqual(capsRes.body.provider, 'netease');
  assert.strictEqual(capsRes.body.sources[0].channels.length, 6);

  const catRes = await makeRequest(service.app, 'GET', '/api/v1/public-playlists/categories');
  assert.strictEqual(catRes.status, 200);
  assert.strictEqual(catRes.body.ok, true);
  assert.ok(catRes.body.tags['语种']);
  assert.strictEqual(catRes.body.highquality_tags.length, 2);
});

test('Auth workflow: QR creation, status, and check 803 cookie persistence', async (t) => {
  const tempDir = path.join('/tmp', `fn-test-session-${Date.now()}`);
  const sessionFile = path.join(tempDir, 'session.json');

  let savedUnikey = 'test-unikey-12345';
  const mockSdk = {
    login_status: async ({ cookie }) => {
      if (cookie && cookie.includes('MUSIC_U=valid_test_token')) {
        return { body: { data: { profile: { userId: 99999, nickname: 'Tester', vipType: 1 } } } };
      }
      return { body: { data: { profile: null } } };
    },
    login_qr_key: async () => ({ body: { data: { unikey: savedUnikey } } }),
    login_qr_create: async ({ key }) => ({ body: { data: { qrurl: `https://music.163.com/login?codekey=${key}`, qrimg: 'data:image/png;base64,...' } } }),
    login_qr_check: async ({ key }) => ({
      body: { code: 803, cookie: 'MUSIC_U=valid_test_token; __csrf=test_csrf' },
      cookie: ['MUSIC_U=valid_test_token; Path=/', '__csrf=test_csrf; Path=/'],
    }),
  };

  const service = createService({ sdk: mockSdk, sessionFile });

  // Check status before login (anonymous)
  const statusBefore = await makeRequest(service.app, 'GET', '/api/v1/auth/status');
  assert.strictEqual(statusBefore.status, 200);
  assert.strictEqual(statusBefore.body.data.logged_in, false);
  assert.strictEqual(statusBefore.body.data.user_id, 0);

  // Initiate login QR
  const loginRes = await makeRequest(service.app, 'POST', '/api/v1/auth/login');
  assert.strictEqual(loginRes.status, 200);
  assert.strictEqual(loginRes.body.data.unikey, savedUnikey);
  assert.ok(loginRes.body.data.qr_ascii.length > 0);

  // Check QR image endpoint
  const qrPng = await makeRequest(service.app, 'GET', '/api/v1/auth/login/qr.png');
  assert.strictEqual(qrPng.status, 200);
  assert.strictEqual(qrPng.headers['content-type'], 'image/png');
  assert.strictEqual(qrPng.buffer.slice(0, 4).toString('hex'), '89504e47');

  // QR poll 803 (authorized)
  const checkRes = await makeRequest(service.app, 'GET', `/api/v1/auth/login/check?unikey=${savedUnikey}`);
  assert.strictEqual(checkRes.status, 200);
  assert.strictEqual(checkRes.body.data.code, 803);

  // Verify session file persisted on disk with 0600 mode
  assert.ok(fs.existsSync(sessionFile), 'Session file must exist');
  const diskSession = JSON.parse(fs.readFileSync(sessionFile, 'utf-8'));
  assert.strictEqual(diskSession.version, 1);
  assert.ok(diskSession.cookie_str.includes('MUSIC_U=valid_test_token'));

  // Check status after login (should be logged in)
  const statusAfter = await makeRequest(service.app, 'GET', '/api/v1/auth/status');
  assert.strictEqual(statusAfter.status, 200);
  assert.strictEqual(statusAfter.body.data.logged_in, true);
  assert.strictEqual(statusAfter.body.data.user_id, 99999);
});

test('persisted device identity is reused and stale login probes cannot replace a new session', async () => {
  let resolveOldStatus;
  const seenCookies = [];
  const service = createService({
    sdk: {
      login_status: async ({ cookie }) => {
        seenCookies.push(cookie);
        if (seenCookies.length === 1) {
          return new Promise(resolve => { resolveOldStatus = resolve; });
        }
        return { body: { data: { profile: { userId: 2, nickname: 'new' } } } };
      },
    },
    sessionFile: path.join('/tmp', `fn-test-device-${process.pid}-${Date.now()}`, 'session.json'),
  });
  service.saveSession({ cookie_str: 'MUSIC_U=old', device_id: 'device-old' });
  const oldProbe = service.checkLoginStatus(true);
  service.saveSession({ cookie_str: 'MUSIC_U=new', device_id: 'device-new' });
  service.resetCaches();
  resolveOldStatus({ body: { data: { profile: { userId: 1, nickname: 'old' } } } });
  const result = await oldProbe;
  assert.strictEqual(result.user_id, 2);
  assert.match(seenCookies[0], /deviceId=device-old/);
  assert.match(seenCookies[1], /deviceId=device-new/);
  assert.strictEqual((await service.checkLoginStatus()).user_id, 2);
});

test('Artist, Album, Playlist, and Toplist endpoints', async () => {
  const mockSdk = {
    artists: async ({ id, limit }) => ({
      body: {
        artist: { id, name: 'Artist Name' },
        hotSongs: [{ id: 1, name: 'Song 1' }, { id: 2, name: 'Song 2' }],
      },
    }),
    album: async ({ id }) => ({
      body: {
        album: { id, name: 'Album Name' },
        songs: [{ id: 10, name: 'Song 10' }],
      },
    }),
    playlist_detail: async ({ id }) => ({
      body: {
        playlist: {
          id,
          name: 'Playlist Name',
          trackIds: [{ id: 100 }, { id: 200 }],
          trackCount: 2,
        },
      },
    }),
    toplist: async () => ({
      body: {
        list: [{ id: 3778678, name: '热歌榜' }],
      },
    }),
    song_detail: async ({ ids }) => ({
      body: {
        songs: ids.split(',').map(id => ({ id: Number(id), name: `Song ${id}`, fee: 0, dt: 180000 })),
        privileges: ids.split(',').map(id => ({ id: Number(id), maxbr: 999000 })),
      },
    }),
    song_url_v1: async ({ id }) => ({
      body: {
        data: id.split(',').map(value => ({ id: Number(value), url: `http://audio/${value}.mp3`, code: 200, fee: 0 })),
      },
    }),
    login_status: async () => ({ body: { data: { profile: null } } }),
  };

  const service = createService({ sdk: mockSdk });

  // Artist
  const artistRes = await makeRequest(service.app, 'GET', '/api/v1/artist/123?limit=10');
  assert.strictEqual(artistRes.status, 200);
  assert.strictEqual(artistRes.body.artist.id, 123);
  assert.strictEqual(artistRes.body.hotSongs.length, 2);

  // Album
  const albumRes = await makeRequest(service.app, 'GET', '/api/v1/album/456');
  assert.strictEqual(albumRes.status, 200);
  assert.strictEqual(albumRes.body.album.id, 456);
  assert.strictEqual(albumRes.body.songs.length, 1);

  // Playlist
  const plRes = await makeRequest(service.app, 'GET', '/api/v1/playlist/789');
  assert.strictEqual(plRes.status, 200);
  assert.strictEqual(plRes.body.playlist.id, 789);

  // Toplist list
  const tlRes = await makeRequest(service.app, 'GET', '/api/v1/toplist');
  assert.strictEqual(tlRes.status, 200);
  assert.strictEqual(tlRes.body.data.length, 1);

  // Toplist index 0
  const tlIndexRes = await makeRequest(service.app, 'GET', '/api/v1/toplist?index=0');
  assert.strictEqual(tlIndexRes.status, 200);
  assert.strictEqual(tlIndexRes.body.index, 0);
  assert.strictEqual(tlIndexRes.body.data.length, 2);
});

test('User playlists, recommend songs, and public playlist tracks', async () => {
  const mockSdk = {
    login_status: async () => ({
      body: { data: { profile: { userId: 12345, nickname: 'TestUser', vipType: 0 } } },
    }),
    user_playlist: async ({ uid }) => ({
      body: {
        playlist: [
          { id: 901, name: 'My Playlist 1', trackCount: 15, userId: uid, createTime: 1700000000000 },
        ],
      },
    }),
    playlist_detail: async ({ id }) => ({
      body: {
        playlist: {
          id,
          name: 'Pl Name',
          trackIds: [{ id: 501 }, { id: 502 }],
          trackCount: 2,
        },
      },
    }),
    recommend_songs: async () => ({
      body: {
        data: {
          dailySongs: [{ id: 501, name: 'Daily 1', fee: 1 }, { id: 502, name: 'Daily 2', fee: 0 }],
        },
      },
    }),
    song_detail: async ({ ids }) => ({
      body: {
        songs: ids.split(',').map(id => ({
          id: Number(id),
          name: `Track ${id}`,
          fee: Number(id) === 501 ? 1 : 0, // 501 is VIP
          dt: 200000,
        })),
        privileges: ids.split(',').map(id => ({ id: Number(id), maxbr: 320000 })),
      },
    }),
    song_url_v1: async ({ id }) => ({
      body: {
        data: id.split(',').map(value => ({ id: Number(value), url: `http://audio/${value}.mp3`, code: 200, fee: Number(value) === 501 ? 1 : 0 })),
      },
    }),
  };

  const service = createService({ sdk: mockSdk });

  // User playlists
  const upRes = await makeRequest(service.app, 'GET', '/api/v1/user/playlists');
  assert.strictEqual(upRes.status, 200);
  assert.strictEqual(upRes.body.ok, true);
  assert.strictEqual(upRes.body.logged_in, true);
  assert.strictEqual(upRes.body.account_uid, 12345);
  assert.strictEqual(upRes.body.data[0].playlist_id, 901);

  // User playlist tracks (fee=1 has needs_vip=true when vipType=0)
  const uptRes = await makeRequest(service.app, 'GET', '/api/v1/user/playlists/901/tracks');
  assert.strictEqual(uptRes.status, 200);
  assert.strictEqual(uptRes.body.ok, true);
  const track501 = uptRes.body.data.find(t => t.song_id === 501);
  const track502 = uptRes.body.data.find(t => t.song_id === 502);
  assert.strictEqual(track501.needs_vip, true);
  assert.strictEqual(track502.needs_vip, undefined);

  // Recommend songs
  const recRes = await makeRequest(service.app, 'GET', '/api/v1/recommend/songs');
  assert.strictEqual(recRes.status, 200);
  assert.strictEqual(recRes.body.ok, true);
  assert.strictEqual(recRes.body.data.length, 2);

  // Public playlist tracks
  const pptRes = await makeRequest(service.app, 'GET', '/api/v1/public-playlists/901/tracks');
  assert.strictEqual(pptRes.status, 200);
  assert.strictEqual(pptRes.body.ok, true);
  assert.strictEqual(pptRes.body.total, 2);
  assert.strictEqual(pptRes.body.truncated, false);
});

test('QR image and text aliases match original endpoints', async () => {
  const mockSdk = {
    login_qr_key: async () => ({ body: { data: { unikey: 'alias-test-key' } } }),
  };
  const service = createService({ sdk: mockSdk });

  const qr1 = await makeRequest(service.app, 'GET', '/api/v1/auth/login/qr.png');
  const qr2 = await makeRequest(service.app, 'GET', '/api/v1/auth/qr.png');
  assert.strictEqual(qr1.status, 200);
  assert.strictEqual(qr2.status, 200);
  assert.strictEqual(qr1.headers['content-type'], 'image/png');
  assert.strictEqual(qr2.headers['content-type'], 'image/png');
  assert.deepStrictEqual(qr1.buffer, qr2.buffer);

  const txt1 = await makeRequest(service.app, 'GET', '/api/v1/auth/login/qr');
  const txt2 = await makeRequest(service.app, 'GET', '/api/v1/auth/qr');
  assert.strictEqual(txt1.status, 200);
  assert.strictEqual(txt2.status, 200);
  assert.strictEqual(txt1.text, txt2.text);
  assert.ok(txt1.text.endsWith('\n'));
});
