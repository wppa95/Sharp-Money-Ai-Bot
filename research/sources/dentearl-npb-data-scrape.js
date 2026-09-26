/**
 * NPB Schedule & Game Results Scraper
 * 
 * Fetches official regular season schedule and box scores from the NPB web service
 * (npb.jp/bis/eng/{season}/calendar/index_{month}.html), normalizes game outcomes
 * (including tie/draw games and cancellations), and outputs production-grade JSON
 * for the baseball division race visualization.
 * 
 * Includes intelligent caching to prevent excessive network requests to NPB servers:
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

export const NPB_TEAM_MAPPING = {
  // Central League
  'G':  { id: 3001, abbrev: 'YOM',  name: 'Yomiuri Giants', nativeName: '読売ジャイアンツ', league: 'NPB', division: 'Central League' },
  'T':  { id: 3002, abbrev: 'HAN',  name: 'Hanshin Tigers', nativeName: '阪神タイガース', league: 'NPB', division: 'Central League' },
  'DB': { id: 3003, abbrev: 'DeNA', name: 'Yokohama DeNA BayStars', nativeName: '横浜DeNAベイスターズ', league: 'NPB', division: 'Central League' },
  'C':  { id: 3004, abbrev: 'HIR',  name: 'Hiroshima Toyo Carp', nativeName: '広島東洋カープ', league: 'NPB', division: 'Central League' },
  'D':  { id: 3005, abbrev: 'CHU',  name: 'Chunichi Dragons', nativeName: '中日ドラゴンズ', league: 'NPB', division: 'Central League' },
  'S':  { id: 3006, abbrev: 'YAK',  name: 'Tokyo Yakult Swallows', nativeName: '東京ヤクルトスワローズ', league: 'NPB', division: 'Central League' },

  // Pacific League
  'H':  { id: 3007, abbrev: 'SBH',  name: 'Fukuoka SoftBank Hawks', nativeName: '福岡ソフトバンクホークス', league: 'NPB', division: 'Pacific League' },
  'F':  { id: 3008, abbrev: 'NHF',  name: 'Hokkaido Nippon-Ham Fighters', nativeName: '北海道日本ハムファイターズ', league: 'NPB', division: 'Pacific League' },
  'M':  { id: 3009, abbrev: 'LOT',  name: 'Chiba Lotte Marines', nativeName: '千葉ロッテマリーンズ', league: 'NPB', division: 'Pacific League' },
  'E':  { id: 3010, abbrev: 'RAK',  name: 'Tohoku Rakuten Golden Eagles', nativeName: '東北楽天ゴールデンイーグルス', league: 'NPB', division: 'Pacific League' },
  'B':  { id: 3011, abbrev: 'ORX',  name: 'ORIX Buffaloes', nativeName: 'オリックス・バファローズ', league: 'NPB', division: 'Pacific League' },
  'L':  { id: 3012, abbrev: 'SEI',  name: 'Saitama Seibu Lions', nativeName: '埼玉西武ライオンズ', league: 'NPB', division: 'Pacific League' }
};

// Aliases for full names, canonical abbreviations, or common alternative notations
export const NPB_ALIASES = {
  'YOM': 3001,
  'GIANTS': 3001,
  'YOMIURI': 3001,
  'HAN': 3002,
  'TIGERS': 3002,
  'HANSHIN': 3002,
  'DENA': 3003,
  'BAYSTARS': 3003,
  'YOKOHAMA': 3003,
  'HIR': 3004,
  'CARP': 3004,
  'HIROSHIMA': 3004,
  'CHU': 3005,
  'DRAGONS': 3005,
  'CHUNICHI': 3005,
  'YAK': 3006,
  'SWALLOWS': 3006,
  'YAKULT': 3006,
  'SBH': 3007,
  'HAWKS': 3007,
  'SOFTBANK': 3007,
  'NHF': 3008,
  'FIGHTERS': 3008,
  'NIPPON-HAM': 3008,
  'NIPPONHAM': 3008,
  'LOT': 3009,
  'MARINES': 3009,
  'LOTTE': 3009,
  'RAK': 3010,
  'EAGLES': 3010,
  'RAKUTEN': 3010,
  'ORX': 3011,
  'BUFFALOES': 3011,
  'ORIX': 3011,
  'SEI': 3012,
  'LIONS': 3012,
  'SEIBU': 3012
};

/**
 * Resolves raw team abbreviation or name to canonical team object
 */
export function resolveNpbTeam(rawName) {
  if (!rawName) return null;
  const trimmed = rawName.trim().toUpperCase();
  if (NPB_TEAM_MAPPING[trimmed]) {
    return NPB_TEAM_MAPPING[trimmed];
  }

  const aliasId = NPB_ALIASES[trimmed];
  if (aliasId) {
    return Object.values(NPB_TEAM_MAPPING).find(t => t.id === aliasId) || null;
  }

  // Fallback search across names
  for (const team of Object.values(NPB_TEAM_MAPPING)) {
    if (team.name.toUpperCase().includes(trimmed) || team.nativeName.includes(rawName.trim())) {
      return team;
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
 * Fetches or reads from cache raw NPB calendar HTML for a specific month
 */
async function fetchMonthHtml(season, month, options = {}) {
  const {
    cacheDir = path.join(rootDir, '.npb_cache'),
    forceRefresh = false,
    ttlHours = 6
  } = options;

  if (!fs.existsSync(cacheDir)) {
    fs.mkdirSync(cacheDir, { recursive: true });
  }

  const cacheFile = path.join(cacheDir, `calendar_${season}_${month}.html`);
  const now = new Date();
  const currentMonthNum = now.getMonth() + 1; // 1-12
  const currentYear = now.getFullYear();
  const monthNum = parseInt(month, 10);

  // Month 04 includes March and April. Consider month past if year < current or month < current
  const isPastMonth = (parseInt(season, 10) < currentYear) || 
                      (parseInt(season, 10) === currentYear && monthNum < currentMonthNum);

  if (!forceRefresh && fs.existsSync(cacheFile)) {
    try {
      const stats = fs.statSync(cacheFile);
      const ageHours = (Date.now() - stats.mtimeMs) / (1000 * 60 * 60);

      // Past months never change and are kept indefinitely; active months expire after ttlHours
      if (isPastMonth || ageHours < ttlHours) {
        console.log(`  💾 [Cache Hit] ${season}-${month} (${isPastMonth ? 'Immutable past month' : `${ageHours.toFixed(1)}h old < ${ttlHours}h TTL`})`);
        return fs.readFileSync(cacheFile, 'utf-8');
      }
    } catch (err) {
      console.warn(`  ⚠️ Could not read cache for ${season}-${month}: ${err.message}. Refetching.`);
    }
  }

  const url = `https://npb.jp/bis/eng/${season}/calendar/index_${month}.html`;
  console.log(`  🌐 [Network Fetch] Requesting ${url} ...`);
  // Polite courtesy delay before hitting the external server
  await sleep(400);

  const response = await fetch(url, {
    headers: {
      'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
      'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'
    }
  });

  if (!response.ok) {
    if (response.status === 404 && month === '11') {
      return '';
    }
    throw new Error(`NPB server HTTP error ${response.status}: ${response.statusText}`);
  }

  const html = await response.text();
  fs.writeFileSync(cacheFile, html, 'utf-8');
  return html;
}

/**
 * Parses raw NPB calendar HTML table into normalized game objects
 */
export function parseNpbCalendarHtml(html = '', seasonYear = '2026', month = '01') {
  const games = [];

  // Each calendar day is represented inside <td class="stschedule"...> or <td> ... </td>
  const tdRegex = /<td[^>]*>([\s\S]*?)<\/td>/gi;
  let tdMatch;

  while ((tdMatch = tdRegex.exec(html)) !== null) {
    const cellContent = tdMatch[1];

    // Find date link or day number
    // e.g. <div class="teschedate"><a href="/bis/eng/2026/games/gm20260327.html">27</a></div>
    // or <div class="date"><span>27</span></div>
    let officialDate = null;
    const dateLinkMatch = cellContent.match(/<div[^>]*class=["']teschedate["'][^>]*>\s*<a[^>]*href=["'][^"']*gm(\d{8})\.html["'][^>]*>/i);
    if (dateLinkMatch) {
      const dStr = dateLinkMatch[1];
      officialDate = `${dStr.slice(0, 4)}-${dStr.slice(4, 6)}-${dStr.slice(6, 8)}`;
    } else {
      const daySpanMatch = cellContent.match(/<div[^>]*class=["'](?:teschedate|date)["'][^>]*>[\s\S]*?<span>(\d+)<\/span>/i) || cellContent.match(/<span>(\d+)<\/span>/i);
      if (daySpanMatch) {
        const dNum = daySpanMatch[1].padStart(2, '0');
        const mStr = String(month || '01').padStart(2, '0');
        officialDate = `${seasonYear}-${mStr}-${dNum}`;
      }
    }

    if (!officialDate) {
      // Cell without games or unlinked day
      continue;
    }

    // Parse games inside this day cell
    // Format 1 (Completed / Final or Postponed with link):
    // <div><a href="/bis/eng/2026/games/s2026032701085.html">G 3 - 1 T</a></div>
    // or with stage marker: <div class="tescheaten">CS First Stage</div><div><a ...>...</a></div>
    const linkGameRegex = /(?:<div class=["']tescheaten["'][^>]*>([^<]+)<\/div>\s*)?(?:<div>)?\s*<a[^>]*href=["'][^"']*\/games\/s(\d+)\.html["'][^>]*>\s*([A-Za-z]+)\s+([\d*]+)\s*-\s*([\d*]+)\s+([A-Za-z]+)\s*<\/a>/gi;
    let gameMatch;

    while ((gameMatch = linkGameRegex.exec(cellContent)) !== null) {
      const specialStage = gameMatch[1];
      const gameId = gameMatch[2];
      const rawAway = gameMatch[3];
      const awayScoreStr = gameMatch[4];
      const homeScoreStr = gameMatch[5];
      const rawHome = gameMatch[6];

      // Skip non-regular season games (All-Star Games, Climax Series, Nippon Series)
      if (specialStage) {
        continue;
      }

      // Skip NPB All-Star Games (CL vs PL exhibition)
      if ((rawAway === 'CL' && rawHome === 'PL') || (rawAway === 'PL' && rawHome === 'CL')) {
        continue;
      }

      const awayTeam = resolveNpbTeam(rawAway);
      const homeTeam = resolveNpbTeam(rawHome);

      if (!awayTeam || !homeTeam) {
        console.warn(`Could not resolve NPB teams: "${rawAway}" vs "${rawHome}" on ${officialDate}`);
        continue;
      }

      // Check for postponed / cancelled (* - *)
      if (awayScoreStr === '*' || homeScoreStr === '*') {
        games.push({
          gamePk: gameId,
          gameDate: `${officialDate}T18:00:00+09:00`,
          officialDate,
          status: { detailedState: 'Postponed', abstractGameState: 'F' },
          teams: {
            away: {
              team: { id: awayTeam.id, name: awayTeam.name, abbrev: awayTeam.abbrev, league: awayTeam.league, division: awayTeam.division },
              score: null,
              isWinner: false
            },
            home: {
              team: { id: homeTeam.id, name: homeTeam.name, abbrev: homeTeam.abbrev, league: homeTeam.league, division: homeTeam.division },
              score: null,
              isWinner: false
            }
          },
          isTie: false,
          isCancelled: true
        });
        continue;
      }

      const awayScore = parseInt(awayScoreStr, 10);
      const homeScore = parseInt(homeScoreStr, 10);
      const isTie = awayScore === homeScore;
      const awayWin = !isTie && awayScore > homeScore;
      const homeWin = !isTie && homeScore > awayScore;

      games.push({
        gamePk: gameId,
        gameDate: `${officialDate}T18:00:00+09:00`,
        officialDate,
        status: { detailedState: 'Final', abstractGameState: 'F' },
        teams: {
          away: {
            team: { id: awayTeam.id, name: awayTeam.name, abbrev: awayTeam.abbrev, league: awayTeam.league, division: awayTeam.division },
            score: awayScore,
            isWinner: awayWin
          },
          home: {
            team: { id: homeTeam.id, name: homeTeam.name, abbrev: homeTeam.abbrev, league: homeTeam.league, division: homeTeam.division },
            score: homeScore,
            isWinner: homeWin
          }
        },
        isTie
      });
    }

    // Format 2 (Future scheduled game without link):
    // <div>C - DB 18:00</div>
    const scheduledRegex = /(?:<div class=["']tescheaten["'][^>]*>([^<]+)<\/div>\s*)?<div>\s*([A-Za-z]+)\s*-\s*([A-Za-z]+)\s+(\d{1,2}:\d{2})\s*<\/div>/gi;
    let schedMatch;

    while ((schedMatch = scheduledRegex.exec(cellContent)) !== null) {
      if (schedMatch[1]) continue; // Skip non-regular season
      const rawAway = schedMatch[2];
      const rawHome = schedMatch[3];
      const gameTime = schedMatch[4];

      const awayTeam = resolveNpbTeam(rawAway);
      const homeTeam = resolveNpbTeam(rawHome);

      if (!awayTeam || !homeTeam) continue;

      const cleanDate = officialDate.replace(/-/g, '');
      const gameId = `${cleanDate}_${awayTeam.abbrev}_${homeTeam.abbrev}`;

      // Only add if not already added by linked format
      if (!games.some(g => g.officialDate === officialDate && g.teams.away.team.id === awayTeam.id && g.teams.home.team.id === homeTeam.id)) {
        games.push({
          gamePk: gameId,
          gameDate: `${officialDate}T${gameTime}:00+09:00`,
          officialDate,
          status: { detailedState: 'Scheduled', abstractGameState: 'P' },
          teams: {
            away: {
              team: { id: awayTeam.id, name: awayTeam.name, abbrev: awayTeam.abbrev, league: awayTeam.league, division: awayTeam.division },
              score: null,
              isWinner: false
            },
            home: {
              team: { id: homeTeam.id, name: homeTeam.name, abbrev: homeTeam.abbrev, league: homeTeam.league, division: homeTeam.division },
              score: null,
              isWinner: false
            }
          },
          isTie: false
        });
      }
    }
  }

  return games;
}

export const parseNpbCalendarHTML = parseNpbCalendarHtml;

/**
 * Scrapes and compiles full NPB season into static JSON datasets
 */
export async function scrapeNpbSeason(season = '2026', options = {}) {
  const { forceRefresh = false } = options;
  console.log(`🇯🇵 Compiling NPB ${season} Regular Season Data...`);

  // Months active in NPB regular season (04 = Mar/Apr, 05 = May, 06 = Jun, 07 = Jul, 08 = Aug, 09 = Sep, 10 = Oct, 11 = Nov for Olympic-delayed seasons)
  const months = ['04', '05', '06', '07', '08', '09', '10', '11'];
  const allGames = [];

  for (const month of months) {
    try {
      const html = await fetchMonthHtml(season, month, { forceRefresh });
      if (!html) {
        continue;
      }
      const monthGames = parseNpbCalendarHtml(html, season, month);
      console.log(`  Month ${month}: parsed ${monthGames.length} games`);
      allGames.push(...monthGames);
    } catch (err) {
      console.error(`  ❌ Error processing month ${month}:`, err.message);
    }
  }

  // Deduplicate and sort chronologically by officialDate and gamePk
  const uniqueMap = new Map();
  allGames.forEach(g => {
    uniqueMap.set(g.gamePk, g);
  });
  const dedupedGames = Array.from(uniqueMap.values());

  dedupedGames.sort((a, b) => {
    if (a.officialDate !== b.officialDate) {
      return a.officialDate.localeCompare(b.officialDate);
    }
    return String(a.gamePk).localeCompare(String(b.gamePk));
  });

  const finalGames = dedupedGames.filter(g => g.status?.detailedState === 'Final');
  const tieGames = finalGames.filter(g => g.isTie);

  console.log(`\n📊 Season Summary (${season}):`);
  console.log(`   Total Games Recorded: ${dedupedGames.length}`);
  console.log(`   Final Games Completed: ${finalGames.length}`);
  console.log(`   Tie Games: ${tieGames.length}`);

  // Summary per team by division
  const teamRecords = {};
  finalGames.forEach(g => {
    const home = g.teams.home;
    const away = g.teams.away;

    if (!teamRecords[home.team.id]) {
      teamRecords[home.team.id] = { name: home.team.name, division: home.team.division, w: 0, l: 0, t: 0 };
    }
    if (!teamRecords[away.team.id]) {
      teamRecords[away.team.id] = { name: away.team.name, division: away.team.division, w: 0, l: 0, t: 0 };
    }

    if (g.isTie) {
      teamRecords[home.team.id].t += 1;
      teamRecords[away.team.id].t += 1;
    } else if (home.isWinner) {
      teamRecords[home.team.id].w += 1;
      teamRecords[away.team.id].l += 1;
    } else if (away.isWinner) {
      teamRecords[away.team.id].w += 1;
      teamRecords[home.team.id].l += 1;
    }
  });

  const centralTeams = Object.values(teamRecords).filter(t => t.division === 'Central League');
  const pacificTeams = Object.values(teamRecords).filter(t => t.division === 'Pacific League');

  const printDivision = (title, teams) => {
    console.log(`\n🏆 ${title} Standings (W-L-T):`);
    teams
      .sort((a, b) => {
        const pctA = (a.w + a.l) > 0 ? a.w / (a.w + a.l) : 0;
        const pctB = (b.w + b.l) > 0 ? b.w / (b.w + b.l) : 0;
        return pctB - pctA;
      })
      .forEach((t, idx) => {
        const pct = (t.w + t.l) > 0 ? (t.w / (t.w + t.l)).toFixed(3) : '.000';
        console.log(`   ${String(idx + 1).padStart(2, ' ')}. ${t.name.padEnd(28, ' ')} ${t.w}-${t.l}-${t.t}  (${pct})`);
      });
  };

  printDivision('Central League', centralTeams);
  printDivision('Pacific League', pacificTeams);

  // Target paths (supports both standalone scraper repo and full web app repo)
  const targets = [];
  const publicDir = path.join(rootDir, 'public', 'data', 'npb');
  const srcDataDir = path.join(rootDir, 'src', 'data', 'npb');
  const standaloneDir = path.join(rootDir, 'data', 'npb');

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

  const serializedData = JSON.stringify(dedupedGames, null, 2);

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

  // If src/data/npb exists, also keep the embedded JS file in sync for file:// protocol support
  const embeddedJsTarget = path.join(srcDataDir, `${season}.js`);
  if (fs.existsSync(srcDataDir)) {
    const jsContent = `/**
 * Embedded ${season} NPB Schedule & Results Dataset
 * Enables zero-server local execution (file:/// protocol) without CORS restrictions.
 */
(function() {
  const games = ${serializedData};
  if (typeof window !== "undefined") {
    window.NPB_STATIC_DATA = window.NPB_STATIC_DATA || {};
    window.NPB_STATIC_DATA["${season}"] = games;
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
  saveScrapeMetadata(rootDir, 'npb', season, dedupedGames);

  return dedupedGames;
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
if (process.argv[1] && process.argv[1].endsWith('scrapeNPB.js')) {
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
      await scrapeNpbSeason(yr, { forceRefresh });
    }
  })()
    .then(() => {
      console.log('\n✅ NPB data extraction complete.');
      process.exit(0);
    })
    .catch(err => {
      console.error('\n❌ NPB scraping failed:', err);
      process.exit(1);
    });
}
