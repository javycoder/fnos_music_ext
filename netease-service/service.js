/**
 * NetEase music adapter service implementing fnmusic-ext HTTP contract
 * Powered by @neteasecloudmusicapienhanced/api
 */
const express = require('express');
const fs = require('fs');
const path = require('path');
const os = require('os');
const QRCode = require('qrcode');
const axios = require('axios');

const QUALITY_WHITELIST = new Set(['standard', 'higher', 'exhigh', 'lossless', 'hires', 'jymaster', 'sky']);

const QUALITY_SLOTS = [
  ['l', 'standard', 'mp3', 128000],
  ['m', 'higher', 'mp3', 192000],
  ['h', 'exhigh', 'mp3', 320000],
  ['sq', 'lossless', 'flac', 999000],
  ['hr', 'hires', 'flac', 999000],
];

function nonnegativeIntOrZero(val) {
  try {
    const n = parseInt(val, 10);
    return Number.isFinite(n) && n > 0 ? n : 0;
  } catch {
    return 0;
  }
}

function mapQualitySlots(raw) {
  if (!raw || typeof raw !== 'object') return {};
  const out = {};
  for (const [field, tier, ext, nominalBr] of QUALITY_SLOTS) {
    const slot = raw[field];
    if (!slot || typeof slot !== 'object') continue;
    out[tier] = {
      ext,
      file_size: nonnegativeIntOrZero(slot.size),
      br: nonnegativeIntOrZero(slot.br) || nominalBr,
    };
  }
  return out;
}

function mapPrivilegeMaxbr(privilege) {
  if (!privilege || typeof privilege !== 'object') return 0;
  return nonnegativeIntOrZero(privilege.maxbr);
}

function mapSongDetail(item, privilege = null) {
  const sid = item.id || item.song_id || 0;
  const songId = nonnegativeIntOrZero(sid);
  const name = String(item.name || item.title || item.song_name || '');
  let artist = '';
  if (Array.isArray(item.ar)) {
    artist = item.ar.filter(a => a && a.name).map(a => String(a.name)).join(' / ');
  } else if (Array.isArray(item.artists)) {
    artist = item.artists.filter(a => a && a.name).map(a => String(a.name)).join(' / ');
  } else if (item.artist) {
    artist = String(item.artist);
  }
  const al = item.al || item.album || {};
  const albumName = String(al.name || item.album_name || '');
  const albumPicUrl = String(al.picUrl || al.pic_url || item.album_pic_url || '');
  const durationMs = nonnegativeIntOrZero(item.dt || item.duration || 0);
  const fee = nonnegativeIntOrZero(item.fee || 0);
  const priv = privilege || (typeof item.privilege === 'object' ? item.privilege : null);
  return {
    song_id: songId,
    name,
    artist,
    album_name: albumName,
    album_pic_url: albumPicUrl,
    duration_ms: durationMs,
    has_sq: Boolean(item.sq),
    has_hr: Boolean(item.hr),
    qualities: mapQualitySlots(item),
    maxbr: mapPrivilegeMaxbr(priv),
    fee,
  };
}

function cleanLyric(rawLyric) {
  if (!rawLyric || typeof rawLyric !== 'string') return '';
  return rawLyric
    .split(/\r?\n/)
    .filter(line => {
      const trimmed = line.trim();
      if (!trimmed) return true;
      if (trimmed.startsWith('{') && trimmed.endsWith('}')) {
        return false;
      }
      return true;
    })
    .join('\n');
}

function cookieToJson(cookie) {
  if (!cookie) return {};
  if (typeof cookie === 'object') return cookie;
  const out = {};
  cookie.split(';').forEach(pair => {
    const [k, ...v] = pair.trim().split('=');
    if (k) out[decodeURIComponent(k)] = decodeURIComponent(v.join('='));
  });
  return out;
}

function cookieObjToString(cookieObj) {
  if (!cookieObj || typeof cookieObj !== 'object') return '';
  return Object.entries(cookieObj)
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v || '')}`)
    .join('; ');
}

function createService(options = {}) {
  const sdk = options.sdk || require('@neteasecloudmusicapienhanced/api');
  const clock = options.clock || {
    now: () => Date.now(),
    monotonicMs: () => performance.now(),
  };
  const timeoutMs = options.timeoutMs || 10000;
  const cacheMax = options.cacheMax || 4096;

  const dataDir = options.dataDir || process.env.SOURCES_DATA_DIR || path.join(process.cwd(), 'sources-data');
  const defaultSessionFile = path.join(dataDir, 'netease', 'session.json');
  const sessionFile = options.sessionFile || process.env.NETEASE_SESSION_FILE || defaultSessionFile;

  let generation = 0;
  let session = null;
  let isInitializing = false;
  let initPromise = null;
  let isInitialized = false;

  // Playback resolution caches: key -> [data, expireMonotonicMs]
  const playbackCache = new Map();
  const inflightTasks = new Map();

  // State caches
  let cachedLoginState = null; // { loggedIn, expireMonotonicMs, uid, profile }
  let cachedVipState = null;   // { entitled, expireMonotonicMs }

  function loadSessionFromDisk() {
    try {
      if (fs.existsSync(sessionFile)) {
        const content = fs.readFileSync(sessionFile, 'utf-8');
        const data = JSON.parse(content);
        if (data && typeof data === 'object') {
          session = data;
          if (typeof data.generation === 'number') {
            generation = Math.max(generation, data.generation);
          }
          return session;
        }
      }
    } catch (err) {
      // ignore load errors
    }
    return null;
  }

  function saveSessionToDisk(newSessionData) {
    try {
      const dir = path.dirname(sessionFile);
      fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
      try {
        fs.chmodSync(dir, 0o700);
      } catch {}

      const tempFile = `${sessionFile}.${Date.now()}.tmp`;
      fs.writeFileSync(tempFile, JSON.stringify(newSessionData, null, 2), { mode: 0o600 });
      try {
        fs.chmodSync(tempFile, 0o600);
      } catch {}
      fs.renameSync(tempFile, sessionFile);
      session = newSessionData;
    } catch (err) {
      throw new Error(`Failed to persist session: ${err.message}`);
    }
  }

  function getCookieString() {
    if (!session) {
      loadSessionFromDisk();
    }
    if (session) {
      let cookie = session.cookie_str || (session.cookies ? cookieObjToString(session.cookies) : '');
      const deviceId = session.device_id || session.cookies?.deviceId;
      if (deviceId && !/(?:^|;\s*)deviceId=/.test(cookie)) {
        cookie += `${cookie ? '; ' : ''}deviceId=${encodeURIComponent(deviceId)}`;
      }
      if (cookie) return cookie;
    }
    return process.env.NETEASE_COOKIE || '';
  }

  function resetCaches(reason = '') {
    generation += 1;
    playbackCache.clear();
    cachedLoginState = null;
    cachedVipState = null;
  }

  // Initial setup: load session
  loadSessionFromDisk();

  async function ensureInitialized() {
    if (isInitialized) return true;
    if (initPromise) return initPromise;
    initPromise = (async () => {
      try {
        // Ensure SDK temporary directory in dedicated runtime
        const dedicatedTmp = path.join(dataDir, 'netease', 'tmp');
        fs.mkdirSync(dedicatedTmp, { recursive: true, mode: 0o700 });
        if (!process.env.TMPDIR) {
          process.env.TMPDIR = dedicatedTmp;
        }
        if (typeof sdk.generateConfig === 'function') {
          await sdk.generateConfig();
        } else {
          try {
            const genConfig = require('@neteasecloudmusicapienhanced/api/generateConfig');
            if (typeof genConfig === 'function') {
              await genConfig();
            }
          } catch {}
        }
        isInitialized = true;
        return true;
      } catch (err) {
        // initialization warning
        return false;
      } finally {
        initPromise = null;
      }
    })();
    return initPromise;
  }

  async function checkLoginStatus(forceFresh = false) {
    const nowMs = clock.monotonicMs();
    if (!forceFresh && cachedLoginState && cachedLoginState.expireMonotonicMs > nowMs) {
      return cachedLoginState;
    }
    const currentGen = generation;
    try {
      const cookie = getCookieString();
      const res = await sdk.login_status({ cookie, timeout: timeoutMs });
      if (currentGen !== generation) return checkLoginStatus(true);
      const profile = res && res.body && res.body.data && res.body.data.profile ? res.body.data.profile : null;
      const account = res && res.body && res.body.data && res.body.data.account ? res.body.data.account : null;
      const userId = profile && profile.userId ? profile.userId : (account && account.id ? account.id : 0);
      const loggedIn = Boolean(profile && userId);
      const ttlMs = loggedIn ? 300000 : 60000;
      cachedLoginState = {
        loggedIn,
        user_id: userId,
        nickname: profile ? profile.nickname || '' : '',
        profile,
        account,
        expireMonotonicMs: nowMs + ttlMs,
      };
      return cachedLoginState;
    } catch (err) {
      if (currentGen !== generation) return checkLoginStatus(true);
      cachedLoginState = {
        loggedIn: false,
        user_id: 0,
        nickname: '',
        profile: null,
        account: null,
        expireMonotonicMs: nowMs + 60000,
      };
      return cachedLoginState;
    }
  }

  async function checkVipEntitled(forceFresh = false) {
    const nowMs = clock.monotonicMs();
    if (!forceFresh && cachedVipState && cachedVipState.expireMonotonicMs > nowMs) {
      return cachedVipState.entitled;
    }
    const login = await checkLoginStatus(forceFresh);
    let entitled = false;
    if (login && login.profile) {
      entitled = nonnegativeIntOrZero(login.profile.vipType) > 0;
    }
    cachedVipState = {
      entitled,
      expireMonotonicMs: nowMs + 300000,
    };
    return entitled;
  }

  async function markNeedsVip(rows) {
    if (!Array.isArray(rows) || rows.length === 0) return rows;
    const isVip = await checkVipEntitled();
    if (isVip) return rows;
    for (const row of rows) {
      if (row && typeof row === 'object' && row.fee === 1) {
        row.needs_vip = true;
      }
    }
    return rows;
  }

  async function filterPlayableSongIds(ids) {
    if (!Array.isArray(ids) || ids.length === 0) return new Set();
    const cookie = getCookieString();
    let urlsData = null;
    try {
      const res = await sdk.song_url_v1({ id: ids.map(Number).join(','), level: 'standard', cookie, timeout: timeoutMs });
      if (res && res.body && Array.isArray(res.body.data)) {
        urlsData = res.body.data;
      }
    } catch {
      // Query exception should not disguise songs as unusable; fallback to song_url
      try {
        const res = await sdk.song_url({ id: ids.join(','), cookie, timeout: timeoutMs });
        if (res && res.body && Array.isArray(res.body.data)) {
          urlsData = res.body.data;
        }
      } catch {}
    }

    if (!Array.isArray(urlsData)) {
      // Query exception: cannot determine, so don't filter out all
      return new Set(ids.map(Number));
    }

    const login = await checkLoginStatus();
    const playable = new Set();
    for (const item of urlsData) {
      if (!item || typeof item !== 'object') continue;
      const sid = nonnegativeIntOrZero(item.id || item.song_id);
      if (!sid) continue;
      const url = item.url ? String(item.url).trim() : '';
      const code = item.code;
      const fee = nonnegativeIntOrZero(item.fee);
      const freeTrial = item.freeTrialInfo;

      if (!url || code === 404) continue;
      if (freeTrial && Object.keys(freeTrial).length > 0 && freeTrial !== 'null') continue;
      if (!login.loggedIn && fee !== 0 && fee !== 8) continue;

      playable.add(sid);
    }
    return playable;
  }

  async function batchSongDetails(ids) {
    if (!Array.isArray(ids) || ids.length === 0) return [];
    const cookie = getCookieString();
    const parsedIds = ids.map(nonnegativeIntOrZero).filter(x => x > 0);
    if (parsedIds.length === 0) return [];

    const allRows = [];
    for (let i = 0; i < parsedIds.length; i += 100) {
      const batch = parsedIds.slice(i, i + 100);
      const res = await sdk.song_detail({ ids: batch.join(','), cookie, timeout: timeoutMs });
      const songs = res && res.body && Array.isArray(res.body.songs) ? res.body.songs : [];
      const privileges = res && res.body && Array.isArray(res.body.privileges) ? res.body.privileges : [];
      const privMap = new Map();
      for (const p of privileges) {
        if (p && p.id) privMap.set(nonnegativeIntOrZero(p.id), p);
      }
      for (const s of songs) {
        if (s && s.id) {
          const sid = nonnegativeIntOrZero(s.id);
          allRows.push(mapSongDetail(s, privMap.get(sid)));
        }
      }
    }

    const rowMap = new Map();
    for (const r of allRows) {
      rowMap.set(r.song_id, r);
    }
    return parsedIds.map(sid => rowMap.get(sid)).filter(Boolean);
  }

  async function getSongUrlAsync(songId, quality, fresh = false, purpose = 'playback', contextGeneration = 0) {
    const sid = nonnegativeIntOrZero(songId);
    if (!sid) throw new Error('Invalid song_id');
    if (!QUALITY_WHITELIST.has(quality)) {
      const err = new Error(`Invalid quality ${JSON.stringify(quality)}`);
      err.status = 400;
      throw err;
    }
    purpose = String(purpose).trim().toLowerCase() === 'download' ? 'download' : 'playback';
    const ctxGen = Math.max(0, parseInt(contextGeneration, 10) || 0);

    const cacheKey = `${generation}:${sid}:${quality}:${purpose}:${ctxGen}`;
    const nowMs = clock.monotonicMs();

    if (!fresh) {
      const cached = playbackCache.get(cacheKey);
      if (cached && cached.expireMonotonicMs > nowMs) {
        const remainingS = Math.max(0.0, (cached.expireMonotonicMs - nowMs) / 1000.0);
        return {
          ...cached.item,
          reuse_for_s: remainingS,
          expiry_source: 'policy',
        };
      }
    }

    let task = inflightTasks.get(cacheKey);
    if (!task) {
      task = (async () => {
        const currentGen = generation;
        const cookie = getCookieString();
        let res;
        try {
          res = await sdk.song_url_v1({ id: sid, level: quality, cookie, timeout: timeoutMs });
        } catch (upstreamErr) {
          if (upstreamErr.code === 'ECONNABORTED' || upstreamErr.message?.includes('timeout')) {
            const err = new Error('NetEase playback resolution timed out');
            err.status = 504;
            throw err;
          }
          const err = new Error(`NetEase playback request failed: ${upstreamErr.message}`);
          err.status = 502;
          throw err;
        }

        if (currentGen !== generation) {
          const err = new Error('login state changed');
          err.status = 502;
          throw err;
        }

        if (!res || !res.body || !Array.isArray(res.body.data)
            || (res.body.code && res.body.code !== 200)) {
          const err = new Error('invalid playback response');
          err.status = 502;
          throw err;
        }
        const rows = res.body.data;
        const item = rows.length > 0 && rows[0] && typeof rows[0] === 'object'
          ? rows[0]
          : { id: sid, code: 404, url: null };

        if (item.code && item.code !== 200 && item.code !== 404) {
          const err = new Error('invalid playback status');
          err.status = 502;
          throw err;
        }
        if (item.url && !String(item.url).startsWith('http://') && !String(item.url).startsWith('https://')) {
          const err = new Error('invalid playback URL');
          err.status = 502;
          throw err;
        }

        const expi = Number(item.expi) || 0;
        let ttlS = 300;
        if (item.url) {
          ttlS = expi > 0 ? Math.min(Math.max(expi - 60, 1), 600) : 300;
        } else {
          ttlS = 60;
        }

        const expireMonotonicMs = clock.monotonicMs() + ttlS * 1000;
        if (playbackCache.size >= cacheMax) {
          const oldestKey = playbackCache.keys().next().value;
          playbackCache.delete(oldestKey);
        }
        playbackCache.set(cacheKey, { item, expireMonotonicMs });

        return {
          ...item,
          reuse_for_s: ttlS,
          expiry_source: 'policy',
        };
      })();

      inflightTasks.set(cacheKey, task);
      const clearTask = () => {
        if (inflightTasks.get(cacheKey) === task) {
          inflightTasks.delete(cacheKey);
        }
      };
      task.then(clearTask, clearTask);
    }

    return task;
  }

  function publicSummary(item, kind = 'playlist') {
    if (!item || typeof item !== 'object') return {};
    const creator = item.creator && typeof item.creator === 'object' ? item.creator : {};
    const cover = String(
      item.coverImgUrl || item.cover_url || item.picUrl || item.cover || ''
    );
    let updated = 0;
    try {
      const rawUp = item.updateTime || item.trackNumberUpdateTime || 0;
      updated = Math.floor(nonnegativeIntOrZero(rawUp) / 1000);
    } catch {}
    return {
      id: String(item.id || ''),
      kind,
      source: 'netease',
      name: String(item.name || ''),
      cover_url: cover,
      track_count: nonnegativeIntOrZero(item.trackCount || item.trackcount || 0),
      author: String(item.nickname || creator.nickname || ''),
      description: String(item.description || item.copywriter || ''),
      updated_at: updated,
    };
  }

  // Setup Express App
  const app = express();
  app.use(express.json());
  app.use(express.urlencoded({ extended: true }));

  // 1. Health check
  app.get('/healthz', (req, res) => {
    res.json({
      status: 'ok',
      source: 'netease',
      provider: 'netease',
      upstream: '@neteasecloudmusicapienhanced/api',
      version: '4.41.1',
    });
  });

  // 2. Search
  app.get('/api/v1/search', async (req, res, next) => {
    try {
      const keyword = String(req.query.keyword || '').trim();
      if (!keyword) {
        return res.status(400).json({ detail: 'keyword cannot be empty' });
      }
      const typeStr = String(req.query.type || 'song').trim();
      const validTypes = new Set(['song', 'album', 'artist', 'playlist']);
      if (!validTypes.has(typeStr)) {
        return res.status(400).json({ detail: `Invalid type '${typeStr}'` });
      }
      const limit = Math.min(Math.max(parseInt(req.query.limit, 10) || 20, 1), 100);
      const offset = Math.max(parseInt(req.query.offset, 10) || 0, 0);

      const typeMap = { song: 1, album: 10, artist: 100, playlist: 1000 };
      const typeCode = typeMap[typeStr] || 1;
      const cookie = getCookieString();

      let searchRes = null;
      let fallbackNeeded = false;

      if (offset > 0) {
        // Deep pagination
        try {
          searchRes = await sdk.cloudsearch({ keywords: keyword, type: typeCode, limit, offset, cookie, timeout: timeoutMs });
        } catch {
          fallbackNeeded = true;
        }
      } else {
        try {
          searchRes = await sdk.cloudsearch({ keywords: keyword, type: typeCode, limit, offset, cookie, timeout: timeoutMs });
          if (!searchRes || !searchRes.body || searchRes.body.code === 405) {
            fallbackNeeded = true;
          }
        } catch {
          fallbackNeeded = true;
        }
      }

      if (fallbackNeeded) {
        try {
          searchRes = await sdk.search({ keywords: keyword, type: typeCode, limit, offset, cookie, timeout: timeoutMs });
        } catch {
          return res.json({ ok: true, code: 200, data: [] });
        }
      }

      const result = searchRes && searchRes.body ? (searchRes.body.result || searchRes.body) : {};
      if (typeStr === 'song') {
        const rawSongs = Array.isArray(result.songs) ? result.songs : [];
        let mapped = rawSongs.map(s => {
          const detail = mapSongDetail(s);
          return {
            ...detail,
            id: detail.song_id,
            song_name: detail.name,
            title: detail.name,
            album: detail.album_name,
            duration: detail.duration_ms / 1000.0,
            quality: detail.has_sq ? 'lossless' : 'exhigh',
          };
        });
        const songIds = mapped.map(x => x.song_id).filter(x => x > 0);
        if (songIds.length > 0) {
          const playable = await filterPlayableSongIds(songIds);
          if (playable.size > 0) {
            mapped = mapped.filter(x => playable.has(x.song_id));
          }
        }
        return res.json({ ok: true, code: 200, data: mapped });
      }
      if (typeStr === 'album') {
        const albums = Array.isArray(result.albums) ? result.albums : [];
        return res.json({ ok: true, code: 200, data: albums });
      }
      if (typeStr === 'artist') {
        const artists = Array.isArray(result.artists) ? result.artists : [];
        return res.json({ ok: true, code: 200, data: artists });
      }
      if (typeStr === 'playlist') {
        const playlists = Array.isArray(result.playlists) ? result.playlists : [];
        return res.json({ ok: true, code: 200, data: playlists });
      }

      return res.json({ ok: true, code: 200, data: [] });
    } catch (err) {
      next(err);
    }
  });

  // 3. Song info
  app.get('/api/v1/song/:song_id/info', async (req, res, next) => {
    try {
      const songId = nonnegativeIntOrZero(req.params.song_id);
      if (!songId) return res.status(400).json({ detail: 'Invalid song_id' });
      const cookie = getCookieString();
      const detailRes = await sdk.song_detail({ ids: String(songId), cookie, timeout: timeoutMs });
      const songs = detailRes && detailRes.body && Array.isArray(detailRes.body.songs) ? detailRes.body.songs : [];
      if (songs.length === 0) {
        return res.status(404).json({ detail: 'Song not found' });
      }
      return res.json({ ok: true, data: songs[0] });
    } catch (err) {
      next(err);
    }
  });

  // 4. Songs detail
  app.get('/api/v1/songs/detail', async (req, res, next) => {
    try {
      const idsParam = String(req.query.ids || '').trim();
      if (!idsParam) {
        return res.status(422).json({ detail: 'ids parameter is required' });
      }
      const tokens = idsParam.split(',').map(s => s.trim());
      const ids = [];
      for (const t of tokens) {
        if (!t) return res.status(422).json({ detail: 'Empty id in ids list' });
        const val = parseInt(t, 10);
        if (!Number.isFinite(val) || val <= 0) {
          return res.status(422).json({ detail: `Invalid id '${t}'` });
        }
        ids.push(val);
      }
      if (ids.length < 1 || ids.length > 100) {
        return res.status(422).json({ detail: 'ids count must be 1..100' });
      }

      const details = await batchSongDetails(ids);
      return res.json({ ok: true, data: details });
    } catch (err) {
      next(err);
    }
  });

  // 5. Song URL
  app.get('/api/v1/song/:song_id/url', async (req, res, next) => {
    try {
      const songId = nonnegativeIntOrZero(req.params.song_id);
      if (!songId) return res.status(400).json({ detail: 'Invalid song_id' });
      const quality = String(req.query.quality || 'exhigh').trim().toLowerCase();
      const fresh = req.query.fresh === '1' || req.query.fresh === 'true';
      const purpose = String(req.query.purpose || 'playback');
      const generationArg = parseInt(req.query.generation, 10) || 0;

      const item = await getSongUrlAsync(songId, quality, fresh, purpose, generationArg);
      return res.json({ ok: true, data: item });
    } catch (err) {
      if (err.status) {
        return res.status(err.status).json({ detail: err.message });
      }
      next(err);
    }
  });

  // 6. Song lyric
  app.get('/api/v1/song/:song_id/lyric', async (req, res, next) => {
    try {
      const songId = nonnegativeIntOrZero(req.params.song_id);
      if (!songId) return res.status(400).json({ detail: 'Invalid song_id' });
      const cookie = getCookieString();
      let rawLrc = '';
      let rawTlrc = '';

      try {
        const resLyric = await sdk.lyric_new({ id: songId, cookie, timeout: timeoutMs });
        if (resLyric && resLyric.body) {
          rawLrc = resLyric.body.lrc?.lyric || '';
          rawTlrc = resLyric.body.tlyric?.lyric || '';
        }
      } catch {
        try {
          const resLyric = await sdk.lyric({ id: songId, cookie, timeout: timeoutMs });
          if (resLyric && resLyric.body) {
            rawLrc = resLyric.body.lrc?.lyric || '';
            rawTlrc = resLyric.body.tlyric?.lyric || '';
          }
        } catch {}
      }

      return res.json({
        ok: true,
        data: {
          lyric: cleanLyric(rawLrc),
          tlyric: cleanLyric(rawTlrc),
        },
      });
    } catch (err) {
      next(err);
    }
  });

  // 7. Artist
  app.get('/api/v1/artist/:artist_id', async (req, res, next) => {
    try {
      const artistId = nonnegativeIntOrZero(req.params.artist_id);
      if (!artistId) return res.status(400).json({ detail: 'Invalid artist_id' });
      const limit = Math.min(Math.max(parseInt(req.query.limit, 10) || 20, 1), 100);
      const cookie = getCookieString();

      const resp = await sdk.artists({ id: artistId, cookie, timeout: timeoutMs });
      const body = resp && resp.body ? resp.body : {};
      const hotSongs = Array.isArray(body.hotSongs) ? body.hotSongs.slice(0, limit) : [];
      return res.json({
        ok: true,
        code: 200,
        data: hotSongs,
        artist: body.artist || {},
        hotSongs,
      });
    } catch (err) {
      next(err);
    }
  });

  // 8. Album
  app.get('/api/v1/album/:album_id', async (req, res, next) => {
    try {
      const albumId = nonnegativeIntOrZero(req.params.album_id);
      if (!albumId) return res.status(400).json({ detail: 'Invalid album_id' });
      const cookie = getCookieString();

      const resp = await sdk.album({ id: albumId, cookie, timeout: timeoutMs });
      const body = resp && resp.body ? resp.body : {};
      const songs = Array.isArray(body.songs) ? body.songs : [];
      return res.json({
        ok: true,
        code: 200,
        data: songs,
        songs,
        album: body.album || {},
      });
    } catch (err) {
      next(err);
    }
  });

  // 9. Playlist
  app.get('/api/v1/playlist/:playlist_id', async (req, res, next) => {
    try {
      const playlistId = nonnegativeIntOrZero(req.params.playlist_id);
      if (!playlistId) return res.status(400).json({ detail: 'Invalid playlist_id' });
      const cookie = getCookieString();

      const resp = await sdk.playlist_detail({ id: playlistId, cookie, timeout: timeoutMs });
      const body = resp && resp.body ? resp.body : {};
      return res.json({
        ok: true,
        code: 200,
        data: body.playlist || {},
        playlist: body.playlist || {},
      });
    } catch (err) {
      next(err);
    }
  });

  // 10. User playlists
  app.get('/api/v1/user/playlists', async (req, res, next) => {
    try {
      const limit = Math.min(Math.max(parseInt(req.query.limit, 10) || 100, 1), 100);
      const login = await checkLoginStatus();
      if (!login.loggedIn || !login.user_id) {
        return res.json({ ok: false, error: 'not_logged_in', logged_in: false });
      }
      const cookie = getCookieString();
      const resp = await sdk.user_playlist({ uid: login.user_id, limit, offset: 0, cookie, timeout: timeoutMs });
      const rows = resp && resp.body && Array.isArray(resp.body.playlist) ? resp.body.playlist : [];
      const playlists = rows.map(it => ({
        playlist_id: nonnegativeIntOrZero(it.id),
        name: String(it.name || ''),
        cover_url: String(it.coverImgUrl || it.cover_url || ''),
        track_count: nonnegativeIntOrZero(it.trackCount || it.track_count || 0),
        created_at: Math.floor(nonnegativeIntOrZero(it.createTime) / 1000) || Math.floor(Date.now() / 1000),
        updated_at: Math.floor(nonnegativeIntOrZero(it.updateTime) / 1000) || Math.floor(Date.now() / 1000),
        user_id: nonnegativeIntOrZero(it.userId || it.user_id || 0),
        special_type: nonnegativeIntOrZero(it.specialType || it.special_type || 0),
      })).filter(x => x.playlist_id > 0);

      return res.json({
        ok: true,
        data: playlists,
        account_uid: login.user_id,
        logged_in: true,
      });
    } catch (err) {
      next(err);
    }
  });

  // 11. User playlist tracks
  app.get('/api/v1/user/playlists/:playlist_id/tracks', async (req, res, next) => {
    try {
      const playlistId = nonnegativeIntOrZero(req.params.playlist_id);
      if (!playlistId) return res.status(400).json({ detail: 'Invalid playlist_id' });
      const login = await checkLoginStatus();
      if (!login.loggedIn) {
        return res.json({ ok: false, error: 'not_logged_in', logged_in: false });
      }
      const cookie = getCookieString();
      const resp = await sdk.playlist_detail({ id: playlistId, cookie, timeout: timeoutMs });
      const playlist = resp && resp.body && resp.body.playlist ? resp.body.playlist : null;
      if (!playlist || !Array.isArray(playlist.trackIds)) {
        return res.json({ ok: false, error: 'playlist not found or inaccessible', logged_in: true });
      }
      const trackIds = playlist.trackIds.map(t => (typeof t === 'object' ? t.id : t)).map(nonnegativeIntOrZero).filter(x => x > 0);
      const slicedIds = trackIds.slice(0, 1000);
      const detailed = await batchSongDetails(slicedIds);
      const marked = await markNeedsVip(detailed);
      return res.json({ ok: true, data: marked });
    } catch (err) {
      next(err);
    }
  });

  // 12. Recommend songs
  app.get('/api/v1/recommend/songs', async (req, res, next) => {
    try {
      const limit = Math.min(Math.max(parseInt(req.query.limit, 10) || 30, 10), 60);
      const login = await checkLoginStatus();
      if (!login.loggedIn) {
        return res.json({ ok: false, error: 'not_logged_in', logged_in: false });
      }
      const cookie = getCookieString();
      const resp = await sdk.recommend_songs({ cookie, timeout: timeoutMs });
      const dataRows = resp && resp.body && resp.body.data ? (resp.body.data.dailySongs || resp.body.data) : [];
      const rawIds = Array.isArray(dataRows) ? dataRows.map(x => nonnegativeIntOrZero(x.id || x.song_id)).filter(x => x > 0) : [];
      const slicedIds = rawIds.slice(0, 100);
      const detailed = await batchSongDetails(slicedIds);
      const marked = await markNeedsVip(detailed);
      const playable = await filterPlayableSongIds(marked.map(x => x.song_id));
      const filtered = marked.filter(x => playable.has(x.song_id)).slice(0, limit);
      return res.json({ ok: true, data: filtered, logged_in: true });
    } catch (err) {
      next(err);
    }
  });

  // 13. Toplist
  app.get('/api/v1/toplist', async (req, res, next) => {
    try {
      const rawIndex = req.query.index;
      const index = rawIndex !== undefined && rawIndex !== null && rawIndex !== ''
        ? parseInt(rawIndex, 10)
        : -1;
      const limit = Math.min(Math.max(parseInt(req.query.limit, 10) || 60, 1), 100);
      const cookie = getCookieString();

      const resp = await sdk.toplist({ cookie, timeout: timeoutMs });
      const list = resp && resp.body && Array.isArray(resp.body.list) ? resp.body.list : [];
      if (Number.isNaN(index) || index < 0) {
        return res.json({ ok: true, code: 200, data: list });
      }
      if (index >= list.length) {
        return res.json({ ok: true, data: [], index });
      }
      const targetBoard = list[index];
      const boardId = nonnegativeIntOrZero(targetBoard.id);
      if (!boardId) {
        return res.json({ ok: true, data: [], index });
      }
      const plResp = await sdk.playlist_detail({ id: boardId, cookie, timeout: timeoutMs });
      const pl = plResp && plResp.body && plResp.body.playlist ? plResp.body.playlist : null;
      const trackIds = pl && Array.isArray(pl.trackIds)
        ? pl.trackIds.map(t => (typeof t === 'object' ? t.id : t)).map(nonnegativeIntOrZero).filter(x => x > 0)
        : [];
      const slicedIds = trackIds.slice(0, 100);
      const detailed = await batchSongDetails(slicedIds);
      const marked = await markNeedsVip(detailed);
      const playable = await filterPlayableSongIds(marked.map(x => x.song_id));
      const filtered = marked.filter(x => playable.has(x.song_id)).slice(0, limit);
      return res.json({ ok: true, data: filtered, index });
    } catch (err) {
      next(err);
    }
  });

  // 14. Public playlists capabilities
  app.get('/api/v1/public-playlists/capabilities', async (req, res) => {
    const login = await checkLoginStatus();
    res.json({
      ok: true,
      provider: 'netease',
      sources: [{
        id: 'netease',
        name: '网易云音乐',
        playback_available: true,
        channels: [
          { id: 'recommend', name: '推荐歌单', login_required: false },
          { id: 'top', name: '热门与分类', login_required: false },
          { id: 'highquality', name: '精品歌单', login_required: false },
          { id: 'search', name: '搜索歌单', login_required: false },
          { id: 'charts', name: '排行榜', login_required: false },
          { id: 'account_recommend', name: '账号推荐歌单', login_required: true },
        ],
        logged_in: login.loggedIn,
      }],
    });
  });

  // 15. Public playlists categories
  app.get('/api/v1/public-playlists/categories', async (req, res, next) => {
    try {
      const source = String(req.query.source || 'netease');
      if (!['netease', 'wy', '163'].includes(source)) {
        return res.status(400).json({ detail: 'unsupported source' });
      }
      const kind = String(req.query.kind || 'playlist');
      if (kind === 'chart') {
        return res.json({
          ok: true,
          categories: [{ id: '', name: '全部' }],
          tags: [],
          sorts: [],
        });
      }

      const cookie = getCookieString();
      const catResp = await sdk.playlist_catlist({ cookie, timeout: timeoutMs });
      const catBody = catResp && catResp.body ? catResp.body : {};
      const names = catBody.categories || {};
      const subs = Array.isArray(catBody.sub) ? catBody.sub : [];
      const grouped = {};
      for (const raw of subs) {
        if (!raw || !raw.name) continue;
        const grp = String(names[String(raw.category)] || '其他');
        if (!grouped[grp]) grouped[grp] = [];
        grouped[grp].push({ id: String(raw.name), name: String(raw.name) });
      }

      let highqualityTags = [];
      try {
        const hqResp = await sdk.playlist_highquality_tags({ cookie, timeout: timeoutMs });
        const hqBody = hqResp && hqResp.body ? hqResp.body : {};
        const tags = Array.isArray(hqBody.tags) ? hqBody.tags : [];
        highqualityTags = tags.map(t => ({ id: String(t.name), name: String(t.name) }));
      } catch {}

      const categories = [{ id: '', name: '全部' }];
      for (const [name, rows] of Object.entries(grouped)) {
        categories.push({ id: name, name, list: rows });
      }

      return res.json({
        ok: true,
        categories,
        tags: grouped,
        highquality_tags: highqualityTags,
      });
    } catch (err) {
      next(err);
    }
  });

  // 16. Public playlists
  app.get('/api/v1/public-playlists', async (req, res, next) => {
    try {
      const source = String(req.query.source || 'netease');
      if (!['netease', 'wy', '163'].includes(source)) {
        return res.status(400).json({ detail: 'unsupported source' });
      }
      const channel = String(req.query.channel || 'top');
      const validChannels = new Set(['recommend', 'top', 'highquality', 'search', 'charts', 'account_recommend']);
      if (!validChannels.has(channel)) {
        return res.status(400).json({ detail: `unsupported channel ${channel}` });
      }
      const category = String(req.query.category || '');
      const sort = String(req.query.sort || 'hot');
      const keyword = String(req.query.keyword || '').trim();
      const page = Math.max(parseInt(req.query.page, 10) || 1, 1);
      const limit = Math.min(Math.max(parseInt(req.query.limit, 10) || 30, 1), 50);
      const cursor = String(req.query.cursor || '');
      const cookie = getCookieString();

      if (channel === 'search') {
        if (!keyword) {
          return res.status(400).json({ detail: 'keyword is required for search channel' });
        }
        const offset = (page - 1) * limit;
        const sResp = await sdk.cloudsearch({ keywords: keyword, type: 1000, limit, offset, cookie, timeout: timeoutMs });
        const resObj = sResp && sResp.body && sResp.body.result ? sResp.body.result : {};
        const rows = Array.isArray(resObj.playlists) ? resObj.playlists : [];
        const total = nonnegativeIntOrZero(resObj.playlistCount);
        const items = rows.map(r => publicSummary(r, 'playlist'));
        const hasMore = rows.length > 0 && (total ? page * limit < total : true);
        return res.json({ ok: true, items, raw_count: rows.length, total, has_more: hasMore });
      }

      if (channel === 'charts') {
        const cResp = await sdk.toplist({ cookie, timeout: timeoutMs });
        const rows = cResp && cResp.body && Array.isArray(cResp.body.list) ? cResp.body.list : [];
        const items = rows.map(r => publicSummary(r, 'chart'));
        return res.json({ ok: true, items, total: items.length, has_more: false });
      }

      if (channel === 'recommend') {
        const pResp = await sdk.personalized({ limit: Math.min(limit, 30), cookie, timeout: timeoutMs });
        const rows = pResp && pResp.body && Array.isArray(pResp.body.result) ? pResp.body.result : [];
        const items = rows.map(r => publicSummary(r, 'playlist'));
        return res.json({ ok: true, items, has_more: false });
      }

      if (channel === 'account_recommend') {
        const login = await checkLoginStatus();
        if (!login.loggedIn) {
          return res.json({ ok: false, error: 'not_logged_in', logged_in: false });
        }
        const arResp = await sdk.recommend_resource({ cookie, timeout: timeoutMs });
        if (arResp && arResp.body && arResp.body.code === 301) {
          return res.json({ ok: false, error: 'not_logged_in', logged_in: false });
        }
        const rows = arResp && arResp.body && Array.isArray(arResp.body.recommend) ? arResp.body.recommend : [];
        const items = rows.map(r => publicSummary(r, 'playlist'));
        return res.json({ ok: true, items, has_more: false });
      }

      if (channel === 'highquality') {
        const params = { limit, cookie, timeout: timeoutMs };
        if (category) params.cat = category;
        if (cursor) params.before = cursor;
        const hqResp = await sdk.top_playlist_highquality(params);
        const hqBody = hqResp && hqResp.body ? hqResp.body : {};
        const rows = Array.isArray(hqBody.playlists) ? hqBody.playlists : [];
        let nextCursor = '';
        if (rows.length > 0) {
          const last = rows[rows.length - 1];
          nextCursor = String(last.updateTime || '');
        }
        const items = rows.map(r => publicSummary(r, 'playlist'));
        const total = nonnegativeIntOrZero(hqBody.total);
        const hasMore = rows.length > 0 && Boolean(hqBody.more);
        return res.json({ ok: true, items, total, has_more: hasMore, next_cursor: nextCursor });
      }

      // channel === 'top'
      const offset = (page - 1) * limit;
      const topResp = await sdk.top_playlist({
        cat: category || '全部',
        order: sort === 'new' ? 'new' : 'hot',
        offset,
        limit,
        cookie,
        timeout: timeoutMs,
      });
      const topBody = topResp && topResp.body ? topResp.body : {};
      const rows = Array.isArray(topBody.playlists) ? topBody.playlists : [];
      const total = nonnegativeIntOrZero(topBody.total);
      const items = rows.map(r => publicSummary(r, 'playlist'));
      const hasMore = Boolean(topBody.more) || (total ? page * limit < total : rows.length > 0);
      return res.json({ ok: true, items, total, has_more: hasMore });
    } catch (err) {
      next(err);
    }
  });

  // 17. Public playlist detail
  app.get('/api/v1/public-playlists/:playlist_id', async (req, res, next) => {
    try {
      const playlistId = nonnegativeIntOrZero(req.params.playlist_id);
      if (!playlistId) return res.status(400).json({ detail: 'Invalid playlist_id' });
      const kind = String(req.query.kind || 'playlist');
      const cookie = getCookieString();

      const pResp = await sdk.playlist_detail({ id: playlistId, cookie, timeout: timeoutMs });
      const pl = pResp && pResp.body && pResp.body.playlist ? pResp.body.playlist : null;
      if (!pl || !pl.id) {
        return res.status(404).json({ detail: `playlist ${playlistId} not found` });
      }
      return res.json({ ok: true, data: publicSummary(pl, kind === 'chart' ? 'chart' : 'playlist') });
    } catch (err) {
      next(err);
    }
  });

  // 18. Public playlist tracks
  app.get('/api/v1/public-playlists/:playlist_id/tracks', async (req, res, next) => {
    try {
      const playlistId = nonnegativeIntOrZero(req.params.playlist_id);
      if (!playlistId) return res.status(400).json({ detail: 'Invalid playlist_id' });
      const cookie = getCookieString();

      const pResp = await sdk.playlist_detail({ id: playlistId, cookie, timeout: timeoutMs });
      const pl = pResp && pResp.body && pResp.body.playlist ? pResp.body.playlist : null;
      if (!pl || !Array.isArray(pl.trackIds)) {
        return res.status(502).json({ detail: `playlist ${playlistId} tracks unavailable` });
      }
      const trackIds = pl.trackIds.map(t => (typeof t === 'object' ? t.id : t)).map(nonnegativeIntOrZero).filter(x => x > 0);
      const seen = new Set();
      const dedupedIds = [];
      let truncated = false;
      for (const sid of trackIds) {
        if (seen.has(sid)) continue;
        if (dedupedIds.length >= 1000) {
          truncated = true;
          break;
        }
        seen.add(sid);
        dedupedIds.push(sid);
      }
      const detailed = await batchSongDetails(dedupedIds);
      const marked = await markNeedsVip(detailed);
      const total = Math.max(trackIds.length, nonnegativeIntOrZero(pl.trackCount));
      return res.json({
        ok: true,
        data: marked,
        total,
        truncated: truncated || total > 1000,
      });
    } catch (err) {
      next(err);
    }
  });

  // 19. Auth status
  app.get('/api/v1/auth/status', async (req, res, next) => {
    try {
      const login = await checkLoginStatus(true);
      return res.json({
        ok: true,
        code: 200,
        data: {
          code: 200,
          logged_in: login.loggedIn,
          user_id: login.user_id,
          nickname: login.nickname,
          profile: login.profile,
        },
      });
    } catch (err) {
      next(err);
    }
  });

  // 20. Auth login (QR creation)
  app.post('/api/v1/auth/login', async (req, res, next) => {
    try {
      const keyResp = await sdk.login_qr_key({});
      const unikey = keyResp?.body?.data?.unikey || keyResp?.body?.unikey || '';
      if (!unikey) {
        return res.status(502).json({ error: 'upstream_error', stderr: 'Missing unikey in auth login response' });
      }
      const qrUrl = `https://music.163.com/login?codekey=${unikey}`;
      let qrAscii = '';
      try {
        qrAscii = await QRCode.toString(qrUrl, { type: 'terminal', small: true });
      } catch {}
      let qrimg = '';
      try {
        const createResp = await sdk.login_qr_create({ key: unikey, qrimg: true });
        qrimg = createResp?.body?.data?.qrimg || '';
      } catch {}

      return res.json({
        ok: true,
        code: 200,
        data: {
          code: 200,
          unikey,
          codekey: unikey,
          qr_url: qrUrl,
          qr_ascii: qrAscii,
          qrimg,
        },
      });
    } catch (err) {
      next(err);
    }
  });

  // 21. Auth login check
  app.get('/api/v1/auth/login/check', async (req, res, next) => {
    try {
      const unikey = String(req.query.unikey || '').trim();
      if (!unikey) {
        return res.status(400).json({ detail: 'unikey cannot be empty' });
      }
      const checkResp = await sdk.login_qr_check({ key: unikey });
      const body = checkResp?.body || {};
      const code = body.code;

      if (code === 803) {
        // Login success: extract cookie, persist session atomically, increment generation, clear caches
        let cookieStr = '';
        if (typeof body.cookie === 'string') {
          cookieStr = body.cookie;
        } else if (Array.isArray(checkResp.cookie) && checkResp.cookie.length > 0) {
          cookieStr = checkResp.cookie.join('; ');
        }
        const cookieObj = cookieToJson(cookieStr);
        const nextGen = generation + 1;
        const newSession = {
          version: 1,
          generation: nextGen,
          device_id: global.deviceId || '',
          cookies: cookieObj,
          cookie_str: cookieStr,
          updated_at: Math.floor(clock.now() / 1000),
          expires_at: Math.floor(clock.now() / 1000) + 30 * 86400,
        };
        saveSessionToDisk(newSession);
        resetCaches('login success 803');
      }

      return res.json({
        ok: true,
        code: 200,
        data: body,
      });
    } catch (err) {
      next(err);
    }
  });

  // 22. QR PNG
  const qrPngHandler = async (req, res, next) => {
    try {
      const keyResp = await sdk.login_qr_key({});
      const unikey = keyResp?.body?.data?.unikey || keyResp?.body?.unikey || '';
      if (!unikey) {
        return res.status(502).json({ error: 'upstream_error', stderr: 'Missing unikey in auth login response' });
      }
      const qrUrl = `https://music.163.com/login?codekey=${unikey}`;
      const buf = await QRCode.toBuffer(qrUrl, { type: 'png', margin: 2, width: 256 });
      res.setHeader('Content-Type', 'image/png');
      return res.send(buf);
    } catch (err) {
      next(err);
    }
  };
  app.get('/api/v1/auth/login/qr.png', qrPngHandler);
  app.get('/api/v1/auth/qr.png', qrPngHandler);

  // 23. QR ASCII text
  const qrTextHandler = async (req, res, next) => {
    try {
      const keyResp = await sdk.login_qr_key({});
      const unikey = keyResp?.body?.data?.unikey || keyResp?.body?.unikey || '';
      if (!unikey) {
        return res.status(502).json({ error: 'upstream_error', stderr: 'Missing unikey in auth login response' });
      }
      const qrUrl = `https://music.163.com/login?codekey=${unikey}`;
      let ascii = await QRCode.toString(qrUrl, { type: 'terminal', small: true });
      if (!ascii.endsWith('\n')) ascii += '\n';
      res.setHeader('Content-Type', 'text/plain; charset=utf-8');
      return res.send(ascii);
    } catch (err) {
      next(err);
    }
  };
  app.get('/api/v1/auth/login/qr', qrTextHandler);
  app.get('/api/v1/auth/qr', qrTextHandler);

  // General error handler
  app.use((err, req, res, next) => {
    const status = err.status || 500;
    res.status(status).json({
      ok: false,
      error: err.message || 'Internal Server Error',
      detail: err.message,
    });
  });

  return {
    app,
    ensureInitialized,
    getGeneration: () => generation,
    getSession: () => session,
    resetCaches,
    getSongUrlAsync,
    batchSongDetails,
    filterPlayableSongIds,
    checkLoginStatus,
    checkVipEntitled,
    saveSession: saveSessionToDisk,
    loadSession: loadSessionFromDisk,
  };
}

module.exports = {
  createService,
  mapQualitySlots,
  mapSongDetail,
  cleanLyric,
  QUALITY_WHITELIST,
};
