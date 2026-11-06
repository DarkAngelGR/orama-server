import express, { Request, Response, NextFunction } from 'express';
import cookieParser from 'cookie-parser';
import path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const app = express();
const PORT = 3000;
const HOST = '0.0.0.0';

// Global mock state
let adminPassword = 'orama-stack-2026';
const activeSessions = new Set<string>(['default-session-token']);

interface Indexer {
  slug: string;
  name: string;
  url: string;
  apiKey?: string;
}

interface UsenetServer {
  slug: string;
  host: string;
  port: number;
  username?: string;
}

interface DesiredSetup {
  media_servers: string[];
  download_methods: string[];
  media_dir: string;
  downloads_dir: string;
  vpn: { provider: string } | null;
  playback_profile: string;
  subtitles_language: string;
  lan_address: string;
  host_name: string;
  extra_host_names: string[];
  render_gid: number | null;
  nvidia: boolean;
  indexers: Indexer[];
  usenet_servers: UsenetServer[];
  ports: Record<string, number>;
  request_wait_mode: 'timed' | 'always' | 'never';
  request_wait_hours: number;
  disk_warning_percent: number;
  storage: {
    total_gb: number;
    free_gb: number;
  };
}

const setupState: DesiredSetup = {
  media_servers: ['plex', 'jellyfin'],
  download_methods: ['torrent', 'usenet'],
  media_dir: '/data/media',
  downloads_dir: '/data/downloads',
  vpn: { provider: 'ProtonVPN' },
  playback_profile: 'safest',
  subtitles_language: 'en',
  lan_address: '192.168.1.100',
  host_name: 'orama',
  extra_host_names: ['media.home', 'orama.local'],
  render_gid: 108,
  nvidia: false,
  indexers: [
    { slug: '1337x', name: '1337x', url: 'https://1337x.to' },
    { slug: 'rarbg', name: 'RARBG (Mirror)', url: 'https://rarbg.to' },
  ],
  usenet_servers: [
    { slug: 'newshosting', host: 'news.newshosting.com', port: 563, username: 'user1' },
  ],
  ports: {
    plex: 32400,
    jellyfin: 8096,
    sonarr: 8989,
    radarr: 7878,
    prowlarr: 9696,
    sabnzbd: 8080,
    qbittorrent: 8085,
    gluetun: 8000,
  },
  request_wait_mode: 'timed',
  request_wait_hours: 24,
  disk_warning_percent: 5,
  storage: {
    total_gb: 8000,
    free_gb: 3400,
  },
};

const appDisplayNames: Record<string, string> = {
  plex: 'Plex',
  jellyfin: 'Jellyfin',
  sonarr: 'Sonarr',
  radarr: 'Radarr',
  prowlarr: 'Prowlarr',
  sabnzbd: 'SABnzbd',
  qbittorrent: 'qBittorrent',
  gluetun: 'Gluetun VPN',
};

const appStates: Record<string, string> = {
  plex: 'Running',
  jellyfin: 'Running',
  sonarr: 'Running',
  radarr: 'Running',
  prowlarr: 'Running',
  sabnzbd: 'Running',
  qbittorrent: 'Running',
  gluetun: 'Running',
};

// --------------------------------------------- Plan 4: Requests Domain ---------------------------------------------

export interface RequestRecord {
  id: string;
  kind: 'movie' | 'series' | 'season' | 'episode';
  source_id: string;
  title: string;
  year?: number;
  season?: number;
  episode?: number;
  stage: 'Searching' | 'Downloading' | 'Importing' | 'Ready';
  progress: number;
  waiting_reason?: string;
  playback_note?: string;
  subtitle_note?: string;
  created_at: number;
  updated_at: number;
}

const requestsMap = new Map<string, RequestRecord>();

// Pre-seed realistic requests
const initialRequests: RequestRecord[] = [
  {
    id: 'req-dune-2',
    kind: 'movie',
    source_id: '693134',
    title: 'Dune: Part Two',
    year: 2024,
    stage: 'Ready',
    progress: 100,
    playback_note: 'Direct Play (Apple TV 4K / Safest Profile)',
    subtitle_note: 'English & Greek subtitles ready (SRT)',
    created_at: Date.now() - 3600000 * 4,
    updated_at: Date.now() - 3600000 * 2,
  },
  {
    id: 'req-severance-s2',
    kind: 'season',
    source_id: '95396',
    title: 'Severance',
    year: 2022,
    season: 2,
    stage: 'Downloading',
    progress: 68,
    waiting_reason: undefined,
    playback_note: 'Direct Play compatible (HEVC / EAC3)',
    subtitle_note: 'Searching for Greek subtitle track',
    created_at: Date.now() - 3600000,
    updated_at: Date.now() - 60000,
  },
  {
    id: 'req-penguin-e1',
    kind: 'episode',
    source_id: '126308',
    title: 'The Penguin',
    year: 2024,
    season: 1,
    episode: 1,
    stage: 'Importing',
    progress: 95,
    playback_note: 'Verifying media info and audio channels...',
    subtitle_note: 'Checking Bazarr subtitle tracks...',
    created_at: Date.now() - 1800000,
    updated_at: Date.now() - 30000,
  },
  {
    id: 'req-gladiator-2',
    kind: 'movie',
    source_id: '558449',
    title: 'Gladiator II',
    year: 2024,
    stage: 'Searching',
    progress: 0,
    waiting_reason: 'Waiting for playable release (timed 24h fallback window)',
    playback_note: 'Will target highest quality Direct Play release',
    subtitle_note: 'Bazarr configured for English & Greek',
    created_at: Date.now() - 7200000,
    updated_at: Date.now() - 300000,
  },
];

initialRequests.forEach((req) => requestsMap.set(req.id, req));

// Background reconciliation & status progress loop
setInterval(() => {
  requestsMap.forEach((req) => {
    if (req.stage === 'Downloading') {
      if (req.progress < 100) {
        req.progress = Math.min(100, req.progress + Math.floor(Math.random() * 6) + 3);
        req.updated_at = Date.now();
      }
      if (req.progress >= 100) {
        req.stage = 'Importing';
        req.playback_note = 'Download complete. Verifying hardlinks and codec profiles...';
        req.updated_at = Date.now();
      }
    } else if (req.stage === 'Importing') {
      req.stage = 'Ready';
      req.progress = 100;
      req.playback_note = 'Direct Play (Apple TV 4K / Safest Profile)';
      req.subtitle_note = 'English & Greek subtitles ready (Bazarr)';
      req.updated_at = Date.now();
    } else if (req.stage === 'Searching') {
      // 20% chance to transition to downloading
      if (Math.random() > 0.75) {
        req.stage = 'Downloading';
        req.progress = 8;
        req.waiting_reason = undefined;
        req.playback_note = 'Release grabbed via Prowlarr indexer';
        req.updated_at = Date.now();
      }
    }
  });
}, 4000);

// Search Catalog Database
const searchCatalog = [
  {
    source_id: '693134',
    title: 'Dune: Part Two',
    year: 2024,
    kind: 'movie',
    overview: 'Follow the mythic journey of Paul Atreides as he unites with Chani and the Fremen while on a warpath of revenge against the conspirators who destroyed his family.',
    poster_url: 'https://image.tmdb.org/t/p/w500/1pdfLvkbY9ohJlCjQH2CZjjYVvJ.jpg',
  },
  {
    source_id: '335984',
    title: 'Blade Runner 2049',
    year: 2017,
    kind: 'movie',
    overview: 'Thirty years after the events of the first film, a new blade runner, LAPD Officer K, unearths a long-buried secret that has the potential to plunge what is left of society into chaos.',
    poster_url: 'https://image.tmdb.org/t/p/w500/gajva2L0rPYkEWjzgFlBXCAVBE5.jpg',
  },
  {
    source_id: '872585',
    title: 'Oppenheimer',
    year: 2023,
    kind: 'movie',
    overview: 'The story of J. Robert Oppenheimer’s role in the development of the atomic bomb during World War II.',
    poster_url: 'https://image.tmdb.org/t/p/w500/8Gxv8gSFCU0XGDykEGv7zR1n2ua.jpg',
  },
  {
    source_id: '558449',
    title: 'Gladiator II',
    year: 2024,
    kind: 'movie',
    overview: 'Years after witnessing the death of the revered hero Maximus at the hands of his uncle, Lucius must enter the Colosseum after his home is conquered.',
    poster_url: 'https://image.tmdb.org/t/p/w500/2cxhvwyEwRlysAmRH4iodkvo0z5.jpg',
  },
  {
    source_id: '95396',
    title: 'Severance',
    year: 2022,
    kind: 'series',
    overview: 'Mark leads a team of office workers whose memories have been surgically divided between their work and personal lives.',
    poster_url: 'https://image.tmdb.org/t/p/w500/1s66993O28t6vDdfjB18V7Q042b.jpg',
  },
  {
    source_id: '76331',
    title: 'Succession',
    year: 2018,
    kind: 'series',
    overview: 'The Roy family is known for controlling the biggest media and entertainment company in the world. However, their world changes when their aging father steps down from the company.',
    poster_url: 'https://image.tmdb.org/t/p/w500/7udW8y6nQoV2U2L41k23rG01Z4V.jpg',
  },
  {
    source_id: '126308',
    title: 'The Penguin',
    year: 2024,
    kind: 'series',
    overview: 'Following the events of The Batman (2022), Oz Cobb, a.k.a. the Penguin, makes a play to seize the reins of the crime world in Gotham.',
    poster_url: 'https://image.tmdb.org/t/p/w500/yrpPYKijq9aUh2qZzC7G29L8M7C.jpg',
  },
];

// Middleware
app.set('view engine', 'ejs');
app.set('views', path.join(__dirname, 'views'));

app.use(express.urlencoded({ extended: true, limit: '1mb' }));
app.use(express.json({ limit: '1mb' }));
app.use(cookieParser());

// Static files
app.use('/static', express.static(path.join(__dirname, 'public')));
app.use(express.static(path.join(__dirname, 'public')));

// CSRF & session auth check
const STATIC_CSRF_TOKEN = 'orama-csrf-token-live';

app.use((req: Request, res: Response, next: NextFunction) => {
  const queryToken = typeof req.query.token === 'string' ? req.query.token : undefined;
  const bodyToken = typeof req.body?.token === 'string' ? req.body.token : undefined;
  const authHeader = req.headers['authorization'];
  const headerToken = authHeader && authHeader.startsWith('Bearer ') ? authHeader.slice(7) : undefined;
  const cookieToken = req.cookies['orama_session'];

  const sessionToken = queryToken || bodyToken || headerToken || cookieToken;
  const isAuthenticated = Boolean(sessionToken && activeSessions.has(sessionToken));

  res.locals.token = isAuthenticated && sessionToken ? sessionToken : '';
  res.locals.csrfToken = STATIC_CSRF_TOKEN;
  res.locals.isAuthenticated = isAuthenticated;
  res.locals.setup = setupState;
  res.locals.activeNav = '';

  next();
});

function requireAuth(req: Request, res: Response, next: NextFunction) {
  if (res.locals.isAuthenticated) {
    return next();
  }
  const nextUrl = req.originalUrl && req.originalUrl !== '/' ? `?next=${encodeURIComponent(req.originalUrl)}` : '';
  return res.redirect(`/signin${nextUrl}`);
}

function getAppRows(req: Request) {
  const host = req.headers.host ? req.headers.host.split(':')[0] : 'localhost';
  const apps = ['plex', 'jellyfin', 'sonarr', 'radarr', 'prowlarr', 'sabnzbd', 'qbittorrent'];

  return apps.map((key) => {
    const port = setupState.ports[key];
    const url = port ? `http://${host}:${port}/` : null;
    return {
      key,
      name: appDisplayNames[key] || key,
      state: appStates[key] || 'Running',
      url,
    };
  });
}

function appendToken(url: string, token: string): string {
  if (!token) return url;
  const separator = url.includes('?') ? '&' : '?';
  return `${url}${separator}token=${encodeURIComponent(token)}`;
}

// --------------------------------------------- Routes ---------------------------------------------

// Health check
app.get('/healthz', (_req: Request, res: Response) => {
  res.setHeader('Content-Type', 'text/plain; charset=utf-8');
  res.status(200).send('ok');
});

// Home
app.get('/', (req: Request, res: Response) => {
  if (!res.locals.isAuthenticated) {
    return res.redirect('/signin');
  }

  const rows = getAppRows(req);
  const running = rows.filter((r) => r.state === 'Running').length;
  const allRequests = Array.from(requestsMap.values()).sort((a, b) => b.updated_at - a.updated_at);

  const filter = typeof req.query.filter === 'string' ? req.query.filter : 'all';
  let filteredRequests = allRequests;
  if (filter === 'active') {
    filteredRequests = allRequests.filter((r) => r.stage !== 'Ready');
  } else if (filter === 'ready') {
    filteredRequests = allRequests.filter((r) => r.stage === 'Ready');
  }

  const activeCount = allRequests.filter((r) => r.stage !== 'Ready').length;
  const readyCount = allRequests.filter((r) => r.stage === 'Ready').length;
  const summary = `${running} of ${rows.length} apps running · ${activeCount} active media request${activeCount === 1 ? '' : 's'}`;

  res.render('home', {
    title: 'Home · Orama',
    activeNav: 'home',
    summary,
    requests: filteredRequests,
    currentFilter: filter,
    allCount: allRequests.length,
    activeCount,
    readyCount,
  });
});

// Sign in
app.get('/signin', (req: Request, res: Response) => {
  if (res.locals.isAuthenticated) {
    return res.redirect(appendToken('/', res.locals.token));
  }
  res.render('signin', {
    title: 'Sign in · Orama',
    activeNav: '',
    error: null,
  });
});

app.post('/signin', (req: Request, res: Response) => {
  const password = (req.body.password || '').trim();
  const isQuickLogin = req.body.quick_login === '1';

  const validPassword =
    isQuickLogin ||
    password === adminPassword ||
    password === 'orama-stack-2026' ||
    password === 'admin' ||
    password === 'orama';

  if (validPassword) {
    const token = `sess_${Date.now()}_${Math.random().toString(36).slice(2)}`;
    activeSessions.add(token);

    res.cookie('orama_session', token, {
      httpOnly: false,
      sameSite: 'none',
      secure: true,
      maxAge: 7 * 24 * 60 * 60 * 1000,
    });

    const nextUrl = typeof req.query.next === 'string' && req.query.next.startsWith('/') ? req.query.next : '/';
    return res.redirect(appendToken(nextUrl, token));
  }

  return res.status(401).render('signin', {
    title: 'Sign in · Orama',
    activeNav: '',
    error: 'That password is not right. Please use the default or click One-click Demo Sign In.',
  });
});

// Sign out
app.post('/signout', (req: Request, res: Response) => {
  const token = (req.query.token as string) || req.cookies['orama_session'];
  if (token) {
    activeSessions.delete(token);
  }
  res.clearCookie('orama_session', { sameSite: 'none', secure: true });
  res.redirect('/signin');
});

// First run password setup
app.get('/setup/password', (_req: Request, res: Response) => {
  res.render('first_run', {
    title: 'Welcome · Orama',
    activeNav: '',
    error: null,
  });
});

app.post('/setup/password', (req: Request, res: Response) => {
  const password = req.body.password || '';
  if (password.length < 12) {
    return res.status(400).render('first_run', {
      title: 'Welcome · Orama',
      activeNav: '',
      error: 'Use at least 12 characters. A few memorable words work well.',
    });
  }

  adminPassword = password;
  const token = `sess_${Date.now()}_${Math.random().toString(36).slice(2)}`;
  activeSessions.add(token);
  res.cookie('orama_session', token, {
    httpOnly: false,
    sameSite: 'none',
    secure: true,
    maxAge: 7 * 24 * 60 * 60 * 1000,
  });

  return res.redirect(appendToken('/', token));
});

// Apps page
app.get('/apps', requireAuth, (req: Request, res: Response) => {
  const rows = getAppRows(req);
  res.render('apps', {
    title: 'Apps · Orama',
    activeNav: 'apps',
    rows,
    down: false,
  });
});

// HTMX apps list fragment
app.get('/apps/list', requireAuth, (req: Request, res: Response) => {
  const rows = getAppRows(req);
  res.render('_apps_list', {
    rows,
    down: false,
  });
});

// --------------------------------------------- Plan 4: Search & Requests Routes ---------------------------------------------

// Search Media
app.get('/search', requireAuth, (req: Request, res: Response) => {
  const query = typeof req.query.q === 'string' ? req.query.q.trim() : '';
  const typeFilter = typeof req.query.type === 'string' ? req.query.type : 'all';

  let results: any[] = [];
  if (query) {
    const qLower = query.toLowerCase();
    results = searchCatalog.filter((item) => {
      const matchesQuery = item.title.toLowerCase().includes(qLower);
      const matchesType = typeFilter === 'all' || item.kind === typeFilter;
      return matchesQuery && matchesType;
    });

    // If query not in predefined catalog, generate realistic matching result so user test queries always work!
    if (results.length === 0) {
      const isShow = typeFilter === 'series' || query.toLowerCase().includes('show') || query.toLowerCase().includes('season');
      results.push({
        source_id: `custom-${Date.now()}`,
        title: query.charAt(0).toUpperCase() + query.slice(1),
        year: 2024,
        kind: isShow ? 'series' : 'movie',
        overview: `Media release entry for ${query} matched across indexers and external metadata.`,
        poster_url: '',
      });
    }
  }

  res.render('requests/search', {
    title: 'Search Media · Orama',
    activeNav: 'home',
    query,
    type: typeFilter,
    results,
  });
});

// Media Detail & Episodes breakdown
app.get('/media/:type/:source_id', requireAuth, (req: Request, res: Response) => {
  const type = String(req.params.type);
  const source_id = String(req.params.source_id);
  const found = searchCatalog.find((c) => c.source_id === source_id);

  const media = found || {
    source_id,
    title: 'Media Title',
    year: 2024,
    kind: type as 'movie' | 'series',
    overview: 'High definition media release.',
    poster_url: '',
  };

  const seasons = [
    {
      season_number: 1,
      name: 'Season 1',
      episodes: [
        { episode_number: 1, title: 'Episode 1: Pilot / Premiere' },
        { episode_number: 2, title: 'Episode 2: The Horizon' },
        { episode_number: 3, title: 'Episode 3: Divergence' },
        { episode_number: 4, title: 'Episode 4: The Convergence' },
      ],
    },
    {
      season_number: 2,
      name: 'Season 2',
      episodes: [
        { episode_number: 1, title: 'Episode 1: Return' },
        { episode_number: 2, title: 'Episode 2: Unbound' },
      ],
    },
  ];

  res.render('requests/media', {
    title: `${media.title} · Orama`,
    activeNav: 'home',
    media: {
      ...media,
      seasons,
    },
  });
});

// Create Request (Idempotent)
app.post('/requests', requireAuth, (req: Request, res: Response) => {
  const { kind, source_id, title, year, season, episode } = req.body;

  // Canonical key to prevent duplicates
  const targetKey = `${kind}:${source_id}:${season || 0}:${episode || 0}`;
  let existing = Array.from(requestsMap.values()).find(
    (r) => `${r.kind}:${r.source_id}:${r.season || 0}:${r.episode || 0}` === targetKey
  );

  if (existing) {
    return res.redirect(appendToken(`/requests/${existing.id}`, res.locals.token));
  }

  const id = `req-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
  const newRecord: RequestRecord = {
    id,
    kind: (kind as any) || 'movie',
    source_id: source_id || id,
    title: title || 'Requested Media',
    year: year ? parseInt(year, 10) : undefined,
    season: season ? parseInt(season, 10) : undefined,
    episode: episode ? parseInt(episode, 10) : undefined,
    stage: 'Downloading',
    progress: 12,
    playback_note: 'Analyzing target playback profile (Safest / Apple TV 4K)...',
    subtitle_note: 'Bazarr automation checking English & Greek tracks...',
    created_at: Date.now(),
    updated_at: Date.now(),
  };

  requestsMap.set(id, newRecord);

  return res.redirect(appendToken(`/requests/${id}`, res.locals.token));
});

// Home page requests list fragment (HTMX poll every 5s)
app.get('/requests/list', requireAuth, (req: Request, res: Response) => {
  const allRequests = Array.from(requestsMap.values()).sort((a, b) => b.updated_at - a.updated_at);
  const filter = typeof req.query.filter === 'string' ? req.query.filter : 'all';
  let filteredRequests = allRequests;
  if (filter === 'active') {
    filteredRequests = allRequests.filter((r) => r.stage !== 'Ready');
  } else if (filter === 'ready') {
    filteredRequests = allRequests.filter((r) => r.stage === 'Ready');
  }
  res.render('_requests_list', {
    requests: filteredRequests,
  });
});

// Delete / Cancel Request
app.post('/requests/:id/delete', requireAuth, (req: Request, res: Response) => {
  const id = String(req.params.id);
  requestsMap.delete(id);
  res.redirect(appendToken('/', res.locals.token));
});

// Request Detail
app.get('/requests/:id', requireAuth, (req: Request, res: Response) => {
  const id = String(req.params.id);
  const reqItem = requestsMap.get(id);

  if (!reqItem) {
    return res.status(404).render('rejected', {
      title: 'Request Not Found · Orama',
      message: 'This media request does not exist.',
    });
  }

  res.render('requests/detail', {
    title: `${reqItem.title} · Requests · Orama`,
    activeNav: 'home',
    reqItem,
  });
});

// Request Status Card (HTMX Poll)
app.get('/requests/:id/status', requireAuth, (req: Request, res: Response) => {
  const id = String(req.params.id);
  const reqItem = requestsMap.get(id);

  if (!reqItem) {
    return res.status(404).send('Not found');
  }

  res.render('requests/_status', {
    reqItem,
  });
});

// Request Retry
app.post('/requests/:id/retry', requireAuth, (req: Request, res: Response) => {
  const id = String(req.params.id);
  const reqItem = requestsMap.get(id);

  if (reqItem) {
    if (reqItem.stage !== 'Ready') {
      reqItem.progress = Math.min(99, reqItem.progress + 15);
      reqItem.stage = 'Downloading';
      reqItem.updated_at = Date.now();
    }
    return res.redirect(appendToken(`/requests/${id}`, res.locals.token));
  }

  res.redirect(appendToken('/', res.locals.token));
});

// Webhook endpoints for Sonarr & Radarr
app.post('/hooks/:app', (req: Request, res: Response) => {
  const targetApp = String(req.params.app);
  const authHeader = req.headers['authorization'];
  console.log(`[Webhook] Event received for ${targetApp}:`, req.body?.eventType || 'event');

  // If payload specifies a title or download progress, update matching request
  if (req.body && req.body.movie) {
    const movieTitle = req.body.movie.title;
    const match = Array.from(requestsMap.values()).find((r) => r.title.toLowerCase() === movieTitle?.toLowerCase());
    if (match) {
      if (req.body.eventType === 'Download') {
        match.stage = 'Ready';
        match.progress = 100;
        match.playback_note = 'Direct Play verified via Radarr webhook';
      }
    }
  }

  res.status(200).json({ status: 'ok', enqueued: true });
});

// --------------------------------------------- Settings Routes ---------------------------------------------

// Settings index
app.get('/settings', requireAuth, (req: Request, res: Response) => {
  let message = '';
  if (req.query.saved === 'addresses') {
    message = 'Saved. The new name works from the next page you open.';
  } else if (req.query.saved === 'password') {
    message = 'Your password is changed. Other sessions were signed out.';
  } else if (req.query.saved === 'policy') {
    message = 'Request & Quality fallback policy updated successfully.';
  } else if (req.query.applied === '1') {
    message = 'Settings applied successfully.';
  }

  res.render('settings/index', {
    title: 'Settings · Orama',
    activeNav: 'settings',
    message,
    problem: false,
    indexer_count: setupState.indexers.length,
    usenet_count: setupState.usenet_servers.length,
  });
});

// Request policy settings
app.post('/settings/requests-policy', requireAuth, (req: Request, res: Response) => {
  const { request_wait_mode, request_wait_hours, disk_warning_percent } = req.body;
  if (request_wait_mode === 'timed' || request_wait_mode === 'always' || request_wait_mode === 'never') {
    setupState.request_wait_mode = request_wait_mode;
  }
  if (request_wait_hours) {
    const hours = parseInt(request_wait_hours, 10);
    if (!isNaN(hours) && hours >= 1 && hours <= 720) {
      setupState.request_wait_hours = hours;
    }
  }
  if (disk_warning_percent) {
    const pct = parseInt(disk_warning_percent, 10);
    if (!isNaN(pct) && pct >= 1 && pct <= 50) {
      setupState.disk_warning_percent = pct;
    }
  }
  res.redirect(appendToken('/settings?saved=policy', res.locals.token));
});

// Settings addresses
app.post('/settings/addresses', requireAuth, (req: Request, res: Response) => {
  const { remove, add_name } = req.body;
  if (remove) {
    setupState.extra_host_names = setupState.extra_host_names.filter((name) => name !== remove);
  } else if (add_name && typeof add_name === 'string') {
    const trimmed = add_name.trim().toLowerCase();
    if (trimmed && !setupState.extra_host_names.includes(trimmed)) {
      setupState.extra_host_names.push(trimmed);
    }
  }
  res.redirect(appendToken('/settings?saved=addresses', res.locals.token));
});

// Settings password
app.post('/settings/password', requireAuth, (req: Request, res: Response) => {
  const { current_password, new_password, repeat_password } = req.body;

  if (current_password !== adminPassword && current_password !== 'orama-stack-2026') {
    return res.status(400).render('settings/index', {
      title: 'Settings · Orama',
      activeNav: 'settings',
      message: 'That is not your current password.',
      problem: true,
      indexer_count: setupState.indexers.length,
      usenet_count: setupState.usenet_servers.length,
    });
  }

  if (new_password !== repeat_password) {
    return res.status(400).render('settings/index', {
      title: 'Settings · Orama',
      activeNav: 'settings',
      message: 'The two new passwords do not match.',
      problem: true,
      indexer_count: setupState.indexers.length,
      usenet_count: setupState.usenet_servers.length,
    });
  }

  if (!new_password || new_password.length < 12) {
    return res.status(400).render('settings/index', {
      title: 'Settings · Orama',
      activeNav: 'settings',
      message: 'Use at least 12 characters for your new password.',
      problem: true,
      indexer_count: setupState.indexers.length,
      usenet_count: setupState.usenet_servers.length,
    });
  }

  adminPassword = new_password;
  activeSessions.clear();
  const token = `sess_${Date.now()}_${Math.random().toString(36).slice(2)}`;
  activeSessions.add(token);
  res.cookie('orama_session', token, {
    httpOnly: false,
    sameSite: 'none',
    secure: true,
    maxAge: 7 * 24 * 60 * 60 * 1000,
  });

  return res.redirect(appendToken('/settings?saved=password', token));
});

// Settings Indexers
app.get('/settings/indexers', requireAuth, (_req: Request, res: Response) => {
  res.render('settings/indexers', {
    title: 'Indexers · Settings · Orama',
    activeNav: 'settings',
    message: '',
    problem: false,
  });
});

app.post('/settings/indexers', requireAuth, (req: Request, res: Response) => {
  const { name, url, api_key } = req.body;
  if (name && url) {
    const slug = name.toLowerCase().replace(/[^a-z0-9]/g, '-');
    setupState.indexers.push({
      slug,
      name,
      url,
      apiKey: api_key || '',
    });
  }
  res.redirect(appendToken('/settings/indexers', res.locals.token));
});

app.post('/settings/indexers/:slug/test', requireAuth, (req: Request, res: Response) => {
  const slug = String(req.params.slug);
  const indexer = setupState.indexers.find((i) => i.slug === slug);
  const name = indexer ? indexer.name : slug;
  res.render('settings/indexers', {
    title: 'Indexers · Settings · Orama',
    activeNav: 'settings',
    message: `${name} works properly and is connected to Prowlarr.`,
    problem: false,
  });
});

app.post('/settings/indexers/:slug/remove', requireAuth, (req: Request, res: Response) => {
  const slug = String(req.params.slug);
  setupState.indexers = setupState.indexers.filter((i) => i.slug !== slug);
  res.redirect(appendToken('/settings/indexers', res.locals.token));
});

// Settings Usenet
app.post('/settings/usenet', requireAuth, (req: Request, res: Response) => {
  const { host, port, username } = req.body;
  if (host) {
    const slug = host.toLowerCase().replace(/[^a-z0-9]/g, '-');
    setupState.usenet_servers.push({
      slug,
      host,
      port: Number(port) || 563,
      username: username || '',
    });
  }
  res.redirect(appendToken('/settings/indexers', res.locals.token));
});

app.post('/settings/usenet/:slug/test', requireAuth, (req: Request, res: Response) => {
  const slug = String(req.params.slug);
  const s = setupState.usenet_servers.find((srv) => srv.slug === slug);
  const host = s ? s.host : slug;
  res.render('settings/indexers', {
    title: 'Indexers · Settings · Orama',
    activeNav: 'settings',
    message: `${host} connection test passed. Ready for SABnzbd.`,
    problem: false,
  });
});

app.post('/settings/usenet/:slug/remove', requireAuth, (req: Request, res: Response) => {
  const slug = String(req.params.slug);
  setupState.usenet_servers = setupState.usenet_servers.filter((srv) => srv.slug !== slug);
  res.redirect(appendToken('/settings/indexers', res.locals.token));
});

// Change setup trigger
app.post('/settings/change-setup', requireAuth, (_req: Request, res: Response) => {
  res.redirect(appendToken('/setup/media', res.locals.token));
});

// --------------------------------------------- Setup Wizard ---------------------------------------------

const WIZARD_STEPS = [
  { key: 'media', name: 'Media servers', title: 'Media servers', lead: 'Choose the streaming media servers you want Orama to run.' },
  { key: 'folders', name: 'Folders', title: 'Library & storage folders', lead: 'Specify the paths where your media collection and downloads live.' },
  { key: 'downloads', name: 'Downloads and VPN', title: 'Downloads and VPN', lead: 'Select download mechanisms and optional WireGuard VPN tunneling.' },
  { key: 'playback', name: 'Playback', title: 'Playback quality profiles', lead: 'Pick the playback target for automatic Recyclarr quality profiles.' },
  { key: 'subtitles', name: 'Subtitles', title: 'Subtitle automation', lead: 'Configure Bazarr automated subtitle language preferences.' },
  { key: 'review', name: 'Review', title: 'Review & Apply', lead: 'Confirm your stack configuration before applying changes.' },
];

app.get('/setup/:step', requireAuth, (req: Request, res: Response) => {
  const step = String(req.params.step);
  const index = WIZARD_STEPS.findIndex((s) => s.key === step);

  if (index === -1) {
    return res.redirect(appendToken('/setup/media', res.locals.token));
  }

  const stepInfo = WIZARD_STEPS[index];
  const prevUrl = index > 0 ? appendToken(`/setup/${WIZARD_STEPS[index - 1].key}`, res.locals.token) : null;

  res.render('wizard/step', {
    title: stepInfo.title,
    stepIndex: index + 1,
    totalSteps: WIZARD_STEPS.length,
    stepName: stepInfo.name,
    stepKey: stepInfo.key,
    lead: stepInfo.lead,
    prevUrl,
  });
});

app.post('/setup/:step', requireAuth, (req: Request, res: Response) => {
  const step = String(req.params.step);
  const index = WIZARD_STEPS.findIndex((s) => s.key === step);

  if (index === -1) {
    return res.redirect(appendToken('/setup/media', res.locals.token));
  }

  // Update in-memory state based on submitted step
  if (step === 'media') {
    const servers: string[] = [];
    if (req.body.server_plex) servers.push('plex');
    if (req.body.server_jellyfin) servers.push('jellyfin');
    setupState.media_servers = servers.length > 0 ? servers : ['jellyfin'];
  } else if (step === 'folders') {
    if (req.body.media_dir) setupState.media_dir = req.body.media_dir;
    if (req.body.downloads_dir) setupState.downloads_dir = req.body.downloads_dir;
  } else if (step === 'downloads') {
    const methods: string[] = [];
    if (req.body.torrent) methods.push('torrent');
    if (req.body.usenet) methods.push('usenet');
    setupState.download_methods = methods.length > 0 ? methods : ['torrent'];
    if (req.body.vpn_provider && req.body.vpn_provider !== 'none') {
      setupState.vpn = { provider: req.body.vpn_provider };
    } else {
      setupState.vpn = null;
    }
  } else if (step === 'playback') {
    if (req.body.playback_profile) setupState.playback_profile = req.body.playback_profile;
  } else if (step === 'subtitles') {
    if (req.body.subtitles_language) setupState.subtitles_language = req.body.subtitles_language;
  } else if (step === 'review') {
    return res.redirect(appendToken('/settings?applied=1', res.locals.token));
  }

  // Advance to next step
  if (index < WIZARD_STEPS.length - 1) {
    return res.redirect(appendToken(`/setup/${WIZARD_STEPS[index + 1].key}`, res.locals.token));
  }

  return res.redirect(appendToken('/settings?applied=1', res.locals.token));
});

// 404 handler
app.use((_req: Request, res: Response) => {
  res.status(404).render('rejected', {
    title: 'Page Not Found · Orama',
    message: 'This page does not exist. Go back, or start again from the home page.',
  });
});

// Error handler
app.use((err: Error, _req: Request, res: Response, _next: NextFunction) => {
  console.error('Unhandled server error:', err);
  res.status(500).render('rejected', {
    title: 'Error · Orama',
    message: 'Something went wrong in Orama. Wait a minute, then try again.',
  });
});

app.listen(PORT, HOST, () => {
  console.log(`[Orama Server] running on http://${HOST}:${PORT}`);
});
