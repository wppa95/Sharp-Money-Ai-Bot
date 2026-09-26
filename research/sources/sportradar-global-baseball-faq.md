<!-- Source URL: https://developer.sportradar.com/baseball/reference/global-baseball-faq.md
Retrieved: 2026-09-26 -->

---
updatedAt: 2026-07-15T15:45:14.000Z
---

Fetch the complete documentation index at: https://developer.sportradar.com/baseball/llms.txt. Use this file to discover all available pages before exploring further. Append .md to any documentation page URL to get its markdown version.

# Global Baseball FAQs

Browse by category below. Select any group or question to jump straight to it. Use your browser's find (**Ctrl/Cmd + F**) to search the page.

## Table of Contents

<div style={{
  display: "flex",
  flexWrap: "wrap",
  gap: "16px",
  margin: "20px 0 8px 0"
}}>
  <div style={{
    flex: "1 1 240px",
    minWidth: "220px",
    border: "1px solid var(--md-code-background, #e1e4e8)",
    borderRadius: "8px",
    padding: "16px 18px"
  }}>
    <p style={{ marginTop: 0, marginBottom: "10px", fontSize: "1rem", fontWeight: 600 }}>
      <a href="#getting-started" style={{ textDecoration: "none" }}>Getting Started</a>
    </p>
    <ul style={{ listStyle: "none", paddingLeft: 0, margin: 0 }}>
        <li style={{ margin: "0 0 6px 0" }}><a href="#coverage">Coverage</a></li>
        <li style={{ margin: "0 0 6px 0" }}><a href="#integration">Integration</a></li>
        <li style={{ margin: "0 0 6px 0" }}><a href="#pagination">Pagination</a></li>
    </ul>
  </div>

  <div style={{
    flex: "1 1 240px",
    minWidth: "220px",
    border: "1px solid var(--md-code-background, #e1e4e8)",
    borderRadius: "8px",
    padding: "16px 18px"
  }}>
    <p style={{ marginTop: 0, marginBottom: "10px", fontSize: "1rem", fontWeight: 600 }}>
      <a href="#game-data" style={{ textDecoration: "none" }}>Game Data</a>
    </p>
    <ul style={{ listStyle: "none", paddingLeft: 0, margin: 0 }}>
        <li style={{ margin: "0 0 6px 0" }}><a href="#sport-statuses">Sport Event Statuses</a></li>
        <li style={{ margin: "0 0 6px 0" }}><a href="#event-types">Event Types</a></li>
        <li style={{ margin: "0 0 6px 0" }}><a href="#probabilities">Probabilities</a></li>
    </ul>
  </div>

  <div style={{
    flex: "1 1 240px",
    minWidth: "220px",
    border: "1px solid var(--md-code-background, #e1e4e8)",
    borderRadius: "8px",
    padding: "16px 18px"
  }}>
    <p style={{ marginTop: 0, marginBottom: "10px", fontSize: "1rem", fontWeight: 600 }}>
      <a href="#reference-data" style={{ textDecoration: "none" }}>Reference Data</a>
    </p>
    <ul style={{ listStyle: "none", paddingLeft: 0, margin: 0 }}>
        <li style={{ margin: "0 0 6px 0" }}><a href="#standings">Standings / Tournaments</a></li>
        <li style={{ margin: "0 0 6px 0" }}><a href="#past-season">Past Season Data</a></li>
    </ul>
  </div>
</div>

***

# Getting Started

<div id="coverage" />

## Coverage

<br />

### What leagues do you cover, and at what level?

Coverage information can be found via our <a href="https://coverage-matrix.sportradar.com/" target="_blank">Coverage Matrix</a>.

<br />

### How do I find out the coverage for a particular match?

Find the node called `coverage` in any of the Summary, Lineups, or Timeline feeds. The attribute `coverage.live` reports if Sportradar has live coverage of the match or not.

<br />

### Why do different groups have coverage information for a competition?

The notion of "groups" is versatile — it's used to distinguish playoffs and our competition structures — so coverage is described at the group level for consistency. Generally, though, within a Baseball competition there will be no difference in coverage between groups.

<br />

### How will a sport event behave when it is not covered with live scores?

When a `sport_event` is not covered live, the `status` and `match_status` will remain as `not_started` until results are entered post-match.

```xml
<sport_event_status status="not_started" match_status="not_started"/>
```

<br />

### How are “Live” endpoints handled in the API?

Sport events appear in the feed 10 minutes before the scheduled start time and are removed 10 minutes after the sport event ends. Live endpoints include: [Live Summaries](https://sportradar-group.readme.io/baseball/reference/global-baseball-live-summaries), [Live Timelines](https://sportradar-group.readme.io/baseball/reference/global-baseball-live-timelines), and [Live Timelines Delta](https://sportradar-group.readme.io/baseball/reference/global-baseball-live-timelines-delta).

<br />

### How do I access NCAA Women's Softball?

All softball competitions can be accessed by changing the `sport` parameter from `baseball` to `softball`.

Please note that you must use your Master API Key within your account to access softball data.

<br />

***

<div id="integration" />

## Integration

<br />

### How can I find the values for various enum data points within the API?

Many enum values are listed in the FAQ below. For the most up-to-date values, please see the Schema
section of the OpenAPI specification <a href="https://api.sportradar.com/baseball/trial/v2/openapi/swagger/index.html" target="_blank">here</a>.

<br />

### What format are date fields presented in?

Date values are presented in the [ISO 8601](https://en.wikipedia.org/wiki/ISO_8601) standard format.

**Timestamp fields** are in UTC. These could include scheduled start times or play-by-play event timestamps. Examples: `scheduled="2024-02-11T23:30:00+00:00"`, `created_at="2024-02-11T23:43:20+00:00"`

**Date-only fields** reflect local league convention and are not UTC-adjusted. These could include season start dates and birth dates. Examples: `start_date="2024-08-16"`, `date_of_birth="1984-09-22"`

<br />

### How do I locate the TTL (Time to Live)/cache on an API endpoint?

The cache (in seconds) can be accessed in the returned header information on each RESTful API call, under `cache-control`.

ex. `cache-control: max-age=1, public, s-maxage=1` or
`cache-control: public, must-revalidate, max-age=120`

<br />

***

<div id="pagination" />

## Pagination

<br />

### How can I tell if an endpoint is paginated?

To determine if a RESTful API endpoint uses pagination, visit the endpoint's page on our developer portal, its OpenAPI specification, or check the `X-Result` and `X-Max-Results` headers in the API response.

* `X-Result`: This header indicates the number of items returned for this request.
* `X-Max-Results`: This header specifies the total number of items available for a specified request.

If both headers are present and the response data is truncated (i.e., `X-Result` value is less than `X-Max-Results`), you can infer that pagination is in use. To fetch the remaining data, make use of the `limit` and `start` query string parameters.

In the below Season Summary response, the header specifies 100 sports events are returned (`x-result`), but 381 sport events are available (`x-max-results`). Make an additional API request – such as appending `&start=100` to the end of your call – to obtain additional events.

```text Response Header Sample
< HTTP/2 200
< content-type: application/json
< content-length: 1725369
< date: Wed, 13 Nov 2024 21:06:18 GMT
< x-amzn-requestid: c10fe405-ce81-46fb-a3db-38d153dd2a6c
< x-offset: 0
< x-amz-apigw-id: BNAvVGCUliAEKJA=
< cache-control: max-age=300, public, s-maxage=300
< x-result: 100
< etag: "341dc78002bc578c62a6878519ba0404"
< x-max-results: 381
< link: <https://schemas.sportradar.com/sportsapi/soccer/v4/schemas/season_summaries.json#>; rel="describedBy"
< x-amzn-trace-id: Root=1-673514c8-7a887f81636af0110ecce2d3
< x-amzn-remapped-date: Wed, 13 Nov 2024 21:06:17 GMT
< vary: Accept-Encoding
< x-cache: Hit from cloudfront
< via: 1.1 f2f0cb8191da3bf07a9ca31ece94ab68.cloudfront.net (CloudFront)
< x-amz-cf-pop: IAD61-P4
< x-amz-cf-id: 1cyji-XQZuBlAUsR8QoHdNLWD-jn1HvBC-ShaCMhXl5mMDiFTDRVyQ==
< age: 16
```

<br />

### Why can't I find a particular match in the Daily Summaries, Season Summaries or Sport Events Updated endpoints?

These endpoints support pagination and return a select number of results by default. To return more matches, include an additional query string parameter. Visit an endpoint page for minimum and maximum result values.

<br />

***

[Back to top ↑](#table-of-contents)

***

# Game Data

<div id="sport-statuses" />

## Sport Event Statuses

<br />

### What are the valid `sport_event_status` - `status` values?

Here are the valid `sport_event_status` values and their definitions:

* `not_started` - The match is scheduled to be played
* `match_about_to_start` - The match is about to begin
* `started` - The match has started
* `live` - The match is currently in progress
* `postponed` - The match has been postponed to a future date
* `suspended` - The match began, but has been suspended to a later time
* `delayed` - The match has been temporarily delayed and will be continued
* `cancelled` - The match has been cancelled and will not be played
* `interrupted` - The match began, but coverage has stopped for a short time. Note that match scores may not be updated during this period, the last recorded match score will be displayed instead
* `ended` - The match is over
* `closed` - The match results have been confirmed
* `abandoned` - The match has been abandoned

<br />

### What are the valid `sport_event_status` - `match_status` values?

Here are the valid `match_status` values:

* `not_started`
* `start_delayed`
* `aet`
* `interrupted`
* `ended`
* `postponed`
* `started`
* `abandoned`
* `cancelled`
* `overtime`
* `1st_inning`
* `2nd_inning`
* `3rd_inning`
* `4th_inning`
* `5th_inning`
* `6th_inning`
* `7th_inning`
* `8th_inning`
* `9th_inning`
* `pause`

<br />

***

<div id="event-types" />

## Event Types

<br />

### What are the possible event types logged?

Here are the valid event types:

* `match_started`
* `match_ended`
* `period_start`
* `period_score`
* `break_start`
* `inning_half_start`
* `pitcher_set`
* `batter_set`
* `play_start`
* `play_end`
* `strike`
* `ball`
* `foul_ball`
* `ball_in_play`
* `run_scored`
* `batter_out`
* `runner_out`
* `player_out`
* `player_on_base`
* `substitution`
* `checked_runner`
* `batter_advances`

```xml Sample (XML)
<event id="2182806760" type="player_on_base" time="2025-11-02T01:31:57+00:00" on_base_type="unknown" starting_base="999" ending_base="2" count_balls="1" count_strikes="3" outs="2" errors="0">
    <players>
        <player id="sr:player:1089356" name="Gimenez, Andres" type="runner"/>
    </players>
</event>
<event id="2182806762" type="player_on_base" time="2025-11-02T01:31:57+00:00" on_base_type="unknown" starting_base="999" ending_base="1" count_balls="1" count_strikes="3" outs="2" errors="0">
    <players>
        <player id="sr:player:843127" name="Springer, George" type="runner"/>
    </players>
</event>
<event id="2182806972" type="substitution" time="2025-11-02T01:32:18+00:00" qualifier="away">
    <players>
        <player id="sr:player:2294847" name="Wrobleski, Justin" type="substituted_out"/>
        <player id="sr:player:865116" name="Glasnow, Tyler" type="substituted_in"/>
    </players>
</event>
<event id="2182808252" type="pitch_result" time="2025-11-02T01:35:03+00:00" inning_half="bottom" result="ball" count_balls="1" count_strikes="0" pitch_number="1">
    <players>
        <player id="sr:player:865116" name="Glasnow, Tyler" type="pitcher"/>
        <player id="sr:player:1100805" name="Guerrero Jr., Vladimir" type="batter"/>
    </players>
</event>
<event id="2182808424" type="pitch_result" time="2025-11-02T01:35:21+00:00" inning_half="bottom" result="strike" count_balls="1" count_strikes="1" pitch_number="2">
    <players>
        <player id="sr:player:1100805" name="Guerrero Jr., Vladimir" type="batter"/>
    </players>
</event>
```
```json Sample (JSON)
{
    "id": 2182871366,
    "type": "player_on_base",
    "time": "2025-11-02T04:15:50+00:00",
    "players": [
        {
            "id": "sr:player:1100805",
            "name": "Guerrero Jr., Vladimir",
            "type": "runner"
        }
    ],
    "on_base_type": "unknown",
    "starting_base": 999,
    "ending_base": 3,
    "count_balls": 4,
    "count_strikes": 0,
    "outs": 1,
    "errors": 0
},
{
    "id": 2182871378,
    "type": "player_on_base",
    "time": "2025-11-02T04:15:53+00:00",
    "players": [
        {
            "id": "sr:player:2290897",
            "name": "Barger, Addison",
            "type": "batter"
        }
    ],
    "on_base_type": "walk",
    "starting_base": 0,
    "ending_base": 1,
    "count_balls": 4,
    "count_strikes": 0,
    "outs": 1,
    "errors": 0
},
{
    "id": 2182871698,
    "type": "pitch_result",
    "time": "2025-11-02T04:16:54+00:00",
    "players": [
        {
            "id": "sr:player:1993847",
            "name": "Kirk, Alejandro",
            "type": "batter"
        }
    ],
    "inning_half": "bottom",
    "result": "strike",
    "count_balls": 0,
    "count_strikes": 1,
    "pitch_number": 1
},
{
    "id": 2182871704,
    "type": "pitch_result",
    "time": "2025-11-02T04:16:55+00:00",
    "players": [
        {
            "id": "sr:player:1993847",
            "name": "Kirk, Alejandro",
            "type": "batter"
        }
    ],
    "inning_half": "bottom",
    "result": "strike",
    "count_balls": 0,
    "count_strikes": 2,
    "pitch_number": 2
},
{
    "id": 2182871822,
    "type": "ball_in_play",
    "time": "2025-11-02T04:17:16+00:00",
    "players": [
        {
            "id": "sr:player:1993847",
            "name": "Kirk, Alejandro",
            "type": "batter"
        }
    ],
    "inning_half": "bottom",
    "ball_in_play_type": "unknown"
},
{
    "id": 2182871838,
    "type": "player_out",
    "time": "2025-11-02T04:17:19+00:00",
    "players": [
        {
            "id": "sr:player:2290897",
            "name": "Barger, Addison",
            "type": "runner"
        }
    ],
    "out_type": "force_out",
    "out_base": 1,
    "count_balls": 0,
    "count_strikes": 2,
    "outs": 2,
    "errors": 0
},
```

<br />

***

<div id="probabilities" />

## Probabilities

<br />

### What is the probabilities package?

The probabilities package is an add-on set of feeds that are an extension of the Season Probabilities feed in the main package (which already provides pre-match probabilities for the sport event winner market).

The main features of the Probabilities extension are: **Live Probabilities** that update throughout the game, **Season Outright Probabilities** for the Tournament Winner market, and **Live Probabilities Coverage** indicator for the next 24 hours.

<br />

### What are the valid outcomes for probabilities?

* `home_team_winner`
* `away_team_winner`

<br />

### What are markets, and which ones are available?

A market represents a betting option for which we provide probability data. Over time, additional markets may be added to the API.

Currently, we offer one market: **3-way**, which indicates the probability of each possible outcome — **home win**, **away win**, or **draw**.

<br />

### Is there Live Probability coverage for every game you cover in the Global Baseball API?

Live Probability coverage depends on a number of factors and we can’t guarantee that every game will be covered for any given league.

However, Sportradar aims to cover close to 100% of games from the following Tier 1 leagues:

* Major League Baseball (MLB) - USA
* Nippon Professional Baseball (NPB) - Japan
* KBO League - South Korea

<br />

### Can I utilize the Probabilities extension if I license the Sportradar MLB API?

If you have a license for our [MLB API](https://sportradar-group.readme.io/baseball/reference/mlb-overview) you can also integrate Live Probabilities by utilizing the ID [Mapping Feeds](https://developer.sportradar.com/baseball/reference/global-baseball-mapping-feeds) available in Global Baseball v2 to connect games between the two products.

<br />

***

[Back to top ↑](#table-of-contents)

***

# Reference Data

<div id="standings" />

## Standings / Tournaments

<br />

### What are the possible values for `cup_round` - `state` in the Season Links feed?

Here are the valid `cup_round` - `state` values and their definitions. These can be leveraged to determine the status of a cup round.

* `empty` - A matchup has been created but neither the match details nor the competitors are known.
* `unseeded_fixture` - Match details are known but competitors are unknown.
* `partial_seeded` - One competitor is known.
* `partial_seeded_fixture` - Match details and one competitor are known.
* `seeded` - Both competitors are known.
* `seeded_fixture` - Match details and both competitors are known.
* `unstarted` - Match(es) have been added.
* `on_going` - The first match has started.
* `decided` - The last match has ended.
* `winner` - The winner is known.
* `cancelled` - The matchup has been cancelled.

<br />

### How are group IDs delivered in the stage array with the various types?

With the type of "league" they will have a `sr:league` prefix.
With the type of "cup" they will have a `sr:cup` prefix.

<br />

<div id="past-season" />

## Past Season Data

<br />

### How do I access past seasons results and stats?

Use the <a href="https://developer.sportradar.com/baseball/reference/global-baseball-competition-seasons" target="_blank">Competition Seasons</a> endpoint to locate the `season_id` for the season you want to access. Use that `season_id` to query the various seasons endpoints.

<br />

***

[Back to top ↑](#table-of-contents)

***

<Callout icon="🙋" theme="default">
  ### More questions?
  Reach out to **[mediasupport@sportradar.com](mailto:mediasupport@sportradar.com)** for further assistance.
</Callout>

<br /><br />