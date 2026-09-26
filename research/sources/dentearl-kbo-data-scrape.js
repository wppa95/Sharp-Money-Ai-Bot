/**
 * KBO Schedule & Game Results Scraper
 * 
 * Fetches official regular season schedule and box scores from the KBO web service
 * (koreabaseball.com/ws/Schedule.asmx/GetScheduleList), normalizes game outcomes
 * (including tie/draw games and cancellations), and outputs production-grade JSON
 * for the baseball division race visualization.
 * 
 * Includes intelligent caching to prevent excessive network requests to KBO servers:
 * - Completed past months are cached permanently.
 * - Active / future months are cached with a configurable TTL (default 6 hours).
 * - Only writes output files when a genuine data diff is detected.
 */

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const rootDir = path.resolve(__dirname, '..');

export const KBO_TEAM_MAPPING = {
  'KIA': { id: 2001, abbrev: 'KIA', name: 'KIA Tigers', nativeName: 'KIA 타이거즈' },
  'HT':  { id: 2001, abbrev: 'KIA', name: 'KIA Tigers', nativeName: 'KIA 타이거즈' },
  'KT':  { id: 2002, abbrev: 'KT',  name: 'KT Wiz', nativeName: 'KT 위즈' },
  'LG':  { id: 2003, abbrev: 'LG',  name: 'LG Twins', nativeName: 'LG 트윈스' },
  'NC':  { id: 2004, abbrev: 'NC',  name: 'NC Dinos', nativeName: 'NC 다이노스' },
  'SSG': { id: 2005, abbrev: 'SSG', name: 'SSG Landers', nativeName: 'SSG 랜더스' },
  'SK':  { id: 2005, abbrev: 'SSG', name: 'SSG Landers', nativeName: 'SSG 랜더스' },
  '두산': { id: 2006, abbrev: 'DOO', name: 'Doosan Bears', nativeName: '두산 베어스' },
  'OB':  { id: 2006, abbrev: 'DOO', name: 'Doosan Bears', nativeName: '두산 베어스' },
  '롯데': { id: 2007, abbrev: 'LOT', name: 'Lotte Giants', nativeName: '롯데 자이언츠' },
  'LT':  { id: 2007, abbrev: 'LOT', name: 'Lotte Giants', nativeName: '롯데 자이언츠' },
  '삼성': { id: 2008, abbrev: 'SAM', name: 'Samsung Lions', nativeName: '삼성 라이온즈' },
  'SS':  { id: 2008, abbrev: 'SAM', name: 'Samsung Lions', nativeName: '삼성 라이온즈' },
  '키움': { id: 2009, abbrev: 'KIW', name: 'Kiwoom Heroes', nativeName: '키움 히어로즈' },
  'WO':  { id: 2009, abbrev: 'KIW', name: 'Kiwoom Heroes', nativeName: '키움 히어로즈' },
  '한화': { id: 2010, abbrev: 'HAN', name: 'Hanwha Eagles', nativeName: '한화 이글스' },
  'HH':  { id: 2010, abbrev: 'HAN', name: 'Hanwha Eagles', nativeName: '한화 이글스' }
};

const KBO_ENDPOINT = 'https://www.koreabaseball.com/ws/Schedule.asmx/GetScheduleList';
const KBO_REFERER = 'https://www.koreabaseball.com/Schedule/Schedule.aspx';

/**
 * Resolves raw team identifier (Hangul or Roman abbrev) to canonical team object
 */
export function resolveKboTeam(rawName) {
  if (!rawName) return null;
  const trimmed = rawName.trim();
  const team = KBO_TEAM_MAPPING[trimmed];
  if (team) return team;

  // Partial or fuzzy match fallback
  for (const [key, val] of Object.entries(KBO_TEAM_MAPPING)) {
    if (trimmed.includes(key) || key.includes(trimmed)) {
      return val;
    }
  }
  return null;
}

/**
 * Polite asynchronous delay helper
 */
function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

/**
 * Fetches or reads from cache raw KBO schedule rows for a specific month
 */
async function fetchMonthRaw(season, month, options = {}) {
  const {
    cacheDir = path.join(rootDir, '.kbo_cache'),
    forceRefresh = false,
    ttlHours = 6
  } = options;

  if (!fs.existsSync(cacheDir)) {
    fs.mkdirSync(cacheDir, { recursive: true });
  }

  const cacheFile = path.join(cacheDir, `raw_${season}_${month}.json`);
  const now = new Date();
  const currentMonthStr = String(now.getMonth() + 1).padStart(2, '0');
  const currentYearStr = String(now.getFullYear());
  const isPastMonth = (season < currentYearStr) || (season === currentYearStr && parseInt(month, 10) < parseInt(currentMonthStr, 10));

  if (!forceRefresh && fs.existsSync(cacheFile)) {
    try {
      const stats = fs.statSync(cacheFile);
      const ageHours = (Date.now() - stats.mtimeMs) / (1000 * 60 * 60);

      // Past months never change and are kept indefinitely; active months expire after ttlHours
      if (isPastMonth || ageHours < ttlHours) {
        console.log(`  💾 [Cache Hit] ${season}-${month} (${isPastMonth ? 'Immutable past month' : `${ageHours.toFixed(1)}h old < ${ttlHours}h TTL`})`);
        const cachedContent = fs.readFileSync(cacheFile, 'utf-8');
        return JSON.parse(cachedContent);
      }
    } catch (err) {
      console.warn(`  ⚠️ Could not read cache for ${season}-${month}: ${err.message}. Refetching.`);
    }
  }

  console.log(`  🌐 [Network Fetch] Requesting ${season}-${month} from KBO web service...`);
  // Polite courtesy delay before hitting the external server
  await sleep(400);

  const formBody = new URLSearchParams({
    leId: '1',
    srIdList: '0,9,6',
    seasonId: String(season),
    gameMonth: month,
    teamId: ''
  });

  const response = await fetch(KBO_ENDPOINT, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
      'Referer': KBO_REFERER,
      'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36'
    },
    body: formBody.toString()
  });

  if (!response.ok) {
    throw new Error(`KBO server HTTP error ${response.status}: ${response.statusText}`);
  }

  const data = await response.json();
  fs.writeFileSync(cacheFile, JSON.stringify(data, null, 2), 'utf-8');
  return data;
}

/**
 * Parses raw KBO service response rows into normalized game objects
 */
export function parseKboScheduleRows(rows = [], seasonYear = '2026') {
  const games = [];
  let currentDate = null;

  rows.forEach(r => {
    const rowCells = r.row || [];
    if (rowCells.length === 0) return;

    // Check for date header cell (e.g. "04.01(수)")
    for (const cell of rowCells) {
      const text = cell.Text || '';
      const dateMatch = text.match(/(\d{2})\.(\d{2})\s*\(/);
      if (dateMatch) {
        const mm = dateMatch[1];
        const dd = dateMatch[2];
        currentDate = `${seasonYear}-${mm}-${dd}`;
        break;
      }
    }

    if (!currentDate) return;

    // Extract game time (e.g. "<b>18:30</b>")
    let gameTime = '18:30';
    for (const cell of rowCells) {
      const timeMatch = (cell.Text || '').match(/<b>(\d{2}:\d{2})<\/b>/);
      if (timeMatch) {
        gameTime = timeMatch[1];
        break;
      }
    }

    // Extract game review link & game ID
    let gameId = null;
    for (const cell of rowCells) {
      const linkMatch = (cell.Text || '').match(/gameId=([A-Za-z0-9_]+)/);
      if (linkMatch) {
        gameId = linkMatch[1];
        break;
      }
    }

    // Find the matchup & score cell (contains "vs" or Class "play")
    const playCell = rowCells.find(c => (c.Class === 'play' || (c.Text && c.Text.includes('vs'))));
    if (!playCell) return;

    const playText = playCell.Text || '';

    // Check for cancellation / rainout / postponement across any cell in the row
    const isCancelled = rowCells.some(c => {
      const t = c.Text || '';
      return t.includes('취소') || t.includes('우천') || t.includes('그라운드');
    });

    // Parse team matchup: <span>AWAY</span><em>...</em><span>HOME</span>
    const teamMatch = playText.match(/<span>([^<]+)<\/span><em>([\s\S]*?)<\/em><span>([^<]+)<\/span>/);
    if (!teamMatch) return;

    const rawAwayName = teamMatch[1].trim();
    const emContent = teamMatch[2].trim();
    const rawHomeName = teamMatch[3].trim();

    const awayTeamMeta = resolveKboTeam(rawAwayName);
    const homeTeamMeta = resolveKboTeam(rawHomeName);

    if (!awayTeamMeta || !homeTeamMeta) {
      console.warn(`Could not resolve teams in matchup: "${rawAwayName}" vs "${rawHomeName}"`);
      return;
    }

    if (!gameId) {
      const cleanDate = currentDate.replace(/-/g, '');
      gameId = `${cleanDate}_${awayTeamMeta.abbrev}_${homeTeamMeta.abbrev}`;
    }

    if (isCancelled) {
      games.push({
        gamePk: gameId,
        gameDate: `${currentDate}T${gameTime}:00+09:00`,
        officialDate: currentDate,
        status: { detailedState: 'Postponed', abstractGameState: 'F' },
        teams: {
          away: {
            team: { id: awayTeamMeta.id, name: awayTeamMeta.name, abbrev: awayTeamMeta.abbrev, league: 'KBO', division: 'KBO' },
            score: null,
            isWinner: false
          },
          home: {
            team: { id: homeTeamMeta.id, name: homeTeamMeta.name, abbrev: homeTeamMeta.abbrev, league: 'KBO', division: 'KBO' },
            score: null,
            isWinner: false
          }
        },
        isTie: false,
        isCancelled: true
      });
      return;
    }

    // Parse scores
    // e.g. <span class="lose">2</span><span>vs</span><span class="win">7</span>
    // or <span class="same">5</span><span>vs</span><span class="same">5</span>
    const scoresMatch = emContent.match(/<span[^>]*class=["']?([^"'>]*)["']?[^>]*>(\d+)<\/span>[\s\S]*?<span[^>]*class=["']?([^"'>]*)["']?[^>]*>(\d+)<\/span>/);

    if (!scoresMatch) {
      // Game not yet played or scheduled
      return;
    }

    const awayClass = scoresMatch[1];
    const awayScore = parseInt(scoresMatch[2], 10);
    const homeClass = scoresMatch[3];
    const homeScore = parseInt(scoresMatch[4], 10);

    const isTie = (awayClass.includes('same') || homeClass.includes('same')) || (awayScore === homeScore);
    const homeWin = !isTie && (homeClass.includes('win') || homeScore > awayScore);
    const awayWin = !isTie && (awayClass.includes('win') || awayScore > homeScore);

    games.push({
      gamePk: gameId,
      gameDate: `${currentDate}T${gameTime}:00+09:00`,
      officialDate: currentDate,
      status: { detailedState: 'Final', abstractGameState: 'F' },
      teams: {
        away: {
          team: {
            id: awayTeamMeta.id,
            name: awayTeamMeta.name,
            abbrev: awayTeamMeta.abbrev,
            league: 'KBO',
            division: 'KBO'
          },
          score: awayScore,
          isWinner: awayWin
        },
        home: {
          team: {
            id: homeTeamMeta.id,
            name: homeTeamMeta.name,
            abbrev: homeTeamMeta.abbrev,
            league: 'KBO',
            division: 'KBO'
          },
          score: homeScore,
          isWinner: homeWin
        }
      },
      isTie
    });
  });

  return games;
}

/**
 * Scrapes and compiles full KBO season into static JSON datasets
 */
export async function scrapeKboSeason(season = '2026', options = {}) {
  const { forceRefresh = false } = options;
  console.log(`🇰🇷 Compiling KBO ${season} Regular Season Data...`);

  // Months active in KBO regular season
  const months = ['03', '04', '05', '06', '07', '08', '09', '10'];
  const allGames = [];

  for (const month of months) {
    try {
      const data = await fetchMonthRaw(season, month, { forceRefresh });
      const monthGames = parseKboScheduleRows(data.rows || [], season);
      console.log(`  Month ${month}: parsed ${monthGames.length} valid games`);
      allGames.push(...monthGames);
    } catch (err) {
      console.error(`  ❌ Error processing month ${month}:`, err.message);
    }
  }

  // Sort chronologically by officialDate and gamePk
  allGames.sort((a, b) => {
    if (a.officialDate !== b.officialDate) {
      return a.officialDate.localeCompare(b.officialDate);
    }
    return String(a.gamePk).localeCompare(String(b.gamePk));
  });

  const finalGames = allGames.filter(g => g.status?.detailedState === 'Final');
  const tieGames = finalGames.filter(g => g.isTie);

  console.log(`\n📊 Season Summary (${season}):`);
  console.log(`   Total Games Recorded: ${allGames.length}`);
  console.log(`   Final Games Completed: ${finalGames.length}`);
  console.log(`   Tie Games: ${tieGames.length}`);

  // Summary per team
  const teamRecords = {};
  finalGames.forEach(g => {
    const home = g.teams.home;
    const away = g.teams.away;

    if (!teamRecords[home.team.name]) teamRecords[home.team.name] = { w: 0, l: 0, t: 0 };
    if (!teamRecords[away.team.name]) teamRecords[away.team.name] = { w: 0, l: 0, t: 0 };

    if (g.isTie) {
      teamRecords[home.team.name].t += 1;
      teamRecords[away.team.name].t += 1;
    } else if (home.isWinner) {
      teamRecords[home.team.name].w += 1;
      teamRecords[away.team.name].l += 1;
    } else if (away.isWinner) {
      teamRecords[away.team.name].w += 1;
      teamRecords[home.team.name].l += 1;
    }
  });

  console.log('\n🏆 Team Standings (W-L-T):');
  Object.entries(teamRecords)
    .sort(([, a], [, b]) => {
      const pctA = (a.w + a.l) > 0 ? a.w / (a.w + a.l) : 0;
      const pctB = (b.w + b.l) > 0 ? b.w / (b.w + b.l) : 0;
      return pctB - pctA;
    })
    .forEach(([team, rec], idx) => {
      const pct = (rec.w + rec.l) > 0 ? (rec.w / (rec.w + rec.l)).toFixed(3) : '.000';
      console.log(`   ${String(idx + 1).padStart(2, ' ')}. ${team.padEnd(16, ' ')} ${rec.w}-${rec.l}-${rec.t}  (${pct})`);
    });

  // Target paths (supports both standalone scraper repo and full web app repo)
  const targets = [];
  const publicDir = path.join(rootDir, 'public', 'data', 'kbo');
  const srcDataDir = path.join(rootDir, 'src', 'data', 'kbo');
  const standaloneDir = path.join(rootDir, 'data', 'kbo');

  if (fs.existsSync(path.join(rootDir, 'public'))) {
    targets.push(path.join(publicDir, `${season}.json`));
  }
  if (fs.existsSync(path.join(rootDir, 'src'))) {
    targets.push(path.join(srcDataDir, `${season}.json`));
  }
  if (targets.length === 0 || fs.existsSync(standaloneDir)) {
    targets.push(path.join(standaloneDir, `${season}.json`));
  }

  targets.forEach(targetFile => {
    const parent = path.dirname(targetFile);
    if (!fs.existsSync(parent)) {
      fs.mkdirSync(parent, { recursive: true });
    }
  });

  const serializedData = JSON.stringify(allGames, null, 2);

  targets.forEach(targetFile => {
    if (fs.existsSync(targetFile)) {
      const existingContent = fs.readFileSync(targetFile, 'utf-8');
      if (existingContent === serializedData) {
        console.log(`  ✨ [No Diff] ${path.relative(rootDir, targetFile)} is already up to date.`);
        return;
      }
    }
    fs.writeFileSync(targetFile, serializedData, 'utf-8');
    console.log(`  💾 [Written] ${path.relative(rootDir, targetFile)} (${(serializedData.length / 1024).toFixed(1)} KB)`);
  });

  // If src/data/kbo exists, also keep the embedded JS file in sync for file:// protocol support
  const embeddedJsTarget = path.join(srcDataDir, `${season}.js`);
  if (fs.existsSync(srcDataDir)) {
    const jsContent = `/**
 * Embedded ${season} KBO Schedule & Results Dataset
 * Enables zero-server local execution (file:/// protocol) without CORS restrictions.
 */
(function() {
  const games = ${serializedData};
  if (typeof window !== "undefined") {
    window.KBO_STATIC_DATA = window.KBO_STATIC_DATA || {};
    window.KBO_STATIC_DATA["${season}"] = games;
  }
  if (typeof module !== "undefined" && module.exports) {
    module.exports = games;
  }
})();
`;
    if (fs.existsSync(embeddedJsTarget) && fs.readFileSync(embeddedJsTarget, 'utf-8') === jsContent) {
      console.log(`  ✨ [No Diff] ${path.relative(rootDir, embeddedJsTarget)} is already up to date.`);
    } else {
      fs.writeFileSync(embeddedJsTarget, jsContent, 'utf-8');
      console.log(`  💾 [Written] ${path.relative(rootDir, embeddedJsTarget)} (${(jsContent.length / 1024).toFixed(1)} KB)`);
    }
  }

  // Save scrape metadata with timestamp
  saveScrapeMetadata(rootDir, 'kbo', season, allGames);

  return allGames;
}

/**
 * Writes data/<league>/metadata.json and updates src/constants/scrapeMeta.js if present
 */
function saveScrapeMetadata(rootDir, league, season, games) {
  const now = new Date();
  let ptString = '';
  try {
    const formatter = new Intl.DateTimeFormat('en-US', {
      timeZone: 'America/Los_Angeles',
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', hour12: false
    });
    const parts = formatter.formatToParts(now);
    const getP = t => parts.find(p => p.type === t)?.value || '';
    ptString = `${getP('year')}-${getP('month')}-${getP('day')} ${getP('hour')}:${getP('minute')} PT`;
  } catch (e) {
    ptString = now.toISOString();
  }

  const metaObj = {
    league,
    season: String(season),
    lastUpdated: now.toISOString(),
    lastUpdatedPT: ptString,
    lastScraped: now.toISOString(),
    lastScrapedPT: ptString,
    totalGames: Array.isArray(games) ? games.length : 0,
    completedGames: Array.isArray(games) ? games.filter(g => g.status?.detailedState === 'Final').length : 0
  };

  const serialized = JSON.stringify(metaObj, null, 2);
  const candidateDirs = [
    path.join(rootDir, 'public', 'data', league),
    path.join(rootDir, 'src', 'data', league),
    path.join(rootDir, 'data', league)
  ];

  candidateDirs.forEach(dir => {
    if (fs.existsSync(dir)) {
      const metaPath = path.join(dir, 'metadata.json');
      fs.writeFileSync(metaPath, serialized, 'utf-8');
      console.log(`  ⏱️ [Metadata] ${path.relative(rootDir, metaPath)} (${ptString})`);
    }
  });

  const scrapeMetaFile = path.join(rootDir, 'src', 'constants', 'scrapeMeta.js');
  if (fs.existsSync(scrapeMetaFile)) {
    try {
      let content = fs.readFileSync(scrapeMetaFile, 'utf-8');
      const lgRegex = new RegExp(`(${league}\\s*:\\s*{[\\s\\S]*?lastScraped:\\s*['"])([^'"]+)(['"])`);
      if (lgRegex.test(content)) {
        content = content.replace(lgRegex, `$1${ptString}$3`);
      }
      const isoRegex = new RegExp(`(${league}\\s*:\\s*{[\\s\\S]*?iso:\\s*['"])([^'"]+)(['"])`);
      if (isoRegex.test(content)) {
        content = content.replace(isoRegex, `$1${now.toISOString()}$3`);
      }
      fs.writeFileSync(scrapeMetaFile, content, 'utf-8');
    } catch (e) {}
  }
}

// CLI Execution entry point
if (process.argv[1] && process.argv[1].endsWith('scrapeKBO.js')) {
  const args = process.argv.slice(2);
  let seasons = ['2026'];
  let forceRefresh = false;

  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--season' && args[i + 1]) {
      seasons = [args[i + 1]];
      i++;
    } else if (args[i] === '--seasons' && args[i + 1]) {
      seasons = args[i + 1].split(',').map(s => s.trim()).filter(Boolean);
      i++;
    } else if (args[i] === '--all') {
      seasons = ['2021', '2022', '2023', '2024', '2025', '2026'];
    } else if (args[i] === '--force' || args[i] === '--no-cache') {
      forceRefresh = true;
    }
  }

  (async () => {
    for (const yr of seasons) {
      await scrapeKboSeason(yr, { forceRefresh });
    }
  })()
    .then(() => {
      console.log('\n✅ KBO data extraction complete.');
      process.exit(0);
    })
    .catch(err => {
      console.error('\n❌ KBO scraping failed:', err);
      process.exit(1);
    });
}
