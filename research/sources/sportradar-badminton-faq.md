<!-- Source URL: https://developer.sportradar.com/badminton/reference/badminton-faq.md
Retrieved: 2026-09-26 -->

---
updatedAt: 2026-07-15T15:55:56.000Z
---

Fetch the complete documentation index at: https://developer.sportradar.com/badminton/llms.txt. Use this file to discover all available pages before exploring further. Append .md to any documentation page URL to get its markdown version.

# FAQs

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
        <li style={{ margin: "0 0 6px 0" }}><a href="#past-season">Past Season Data</a></li>
        <li style={{ margin: "0 0 6px 0" }}><a href="#standings">Standings / Tournaments</a></li>
    </ul>
  </div>
</div>

***

# Getting Started

<div id="coverage" />

## Coverage

<br />

### What leagues do you cover, and at what level?

Coverage information can be found via our [Coverage Matrix](https://coverage-matrix.sportradar.com/).

<br />

### How do I find out the coverage for a particular match?

Find the node called `coverage` in any of the Summary, Lineups, or Timeline feeds. The attribute `coverage` - `live` reports if Sportradar has live coverage of the match or not.

<br />

### Why do different groups have coverage information for a competition?

The notion of “groups” is versatile and is used to distinguish between playoffs, and our competition structures and is therefore necessary to describe coverage at a group level for consistency. Generally, however, within a competition in Badminton, there will be no difference between competition coverage between conferences.

<br />

### How will a sport event behave when it is not covered with live scores?

When a `sport_event` is not covered live, the `status` and `match_status` will remain as `not_started` until results are entered post-match.

<br />

***

<div id="integration" />

## Integration

<br />

### How can I find the values for various enum data points within the API?

Many enum values are listed in the FAQ below. For the most up-to-date values, please see the Schema section of the OpenAPI specification [here](https://api.sportradar.com/badminton/trial/v2/openapi/swagger/index.html).

<br />

### What format are date fields presented in?

Date values are presented in the [ISO 8601](https://en.wikipedia.org/wiki/ISO_8601) standard format.

**Timestamp fields** are in UTC. These could include scheduled start times or play-by-play event timestamps. Examples: `scheduled="2024-02-11T23:30:00+00:00"`, `created_at="2024-02-11T23:43:20+00:00"`

**Date-only fields** reflect local league convention and are not UTC-adjusted. These could include season start dates and birth dates. Examples: `start_date="2024-08-16"`, `date_of_birth="1984-09-22"`

<br />

### How do I locate the TTL (Time to Live)/cache on an API endpoint?

The cache (in seconds) can be accessed in the returned header information on each RESTful API call, under `cache-control`.

ex. `cache-control: max-age=1, public, s-maxage=1` or `cache-control: public, must-revalidate, max-age=120`

<br />

***

<div id="pagination" />

## Pagination

<br />

### How can I tell if an endpoint is paginated?

To determine if a RESTful API endpoint uses pagination, visit the endpoint's page on our developer portal, its OpenAPI specification, or check the `X-Result` and `X-Max-Results` headers in the API response.

* `X-Result`: This header indicates the number of items returned for this request.
* `X-Max-Results`: This header specifies the total number of items available for a specified request.

If both headers are present and the response data is truncated (i.e., `X-Result` value is less than `X-Max-Results`), you can infer that pagination is in use. To fetch the remaining data, make use of the `limit` and `start` (or `offset`) query string parameters.

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

These endpoints support pagination and return 200 entries by default. To return more matches, include an additional query string parameter. For example, appending `&start=200` will return the next 200 entries per page, `&start=400` will return the next 200, and so on.

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
* `started` - The match has begun
* `live` - The match is currently in progress
* `postponed` - The match has been postponed to a future date
* `suspended` - The match has been suspended
* `match_about_to_start` - The match is about to begin
* `delayed` - The match has been temporarily delayed and will be continued. Typically appears prior to match start
* `interrupted` - The match began, but coverage has stopped for a short time. Note that match scores may not be updated during this period; the last recorded match score will be displayed instead. Any interrupted match that remains in this status for more than 48 hours will receive limited coverage upon resumption; coverage up to the point of interruption remains at the level originally specified for the match.
* `cancelled` - The match has been cancelled and will not be played
* `ended` - The match is over
* `closed` - The match results have been confirmed

<br />

### What are the valid `sport_event_status` - `match_status` values?

Here are the valid `match_status` values:

* `not_started`
* `match_about_to_start`
* `walkover`
* `ended`
* `postponed`
* `cancelled`
* `1st_set`
* `2nd_set`
* `3rd_set`
* `4th_set`
* `5th_set`
* `6th_set`
* `7th_set`
* `pause`
* `suspended`
* `retired`
* `abandoned`
* `start_delayed`
* `interrupted`
* `started`

<br />

***

<div id="event-types" />

## Event Types

<br />

### What are the point event possible values?

Here are the valid point event values:

* `match_started`
* `period_start`
* `break_start`
* `period_score`
* `score_change`
* `match_ended`

<br />

***

<div id="probabilities" />

## Probabilities

<br />

### What are markets, and which ones are available?

A market represents a betting option for which we provide probability data. Over time, additional markets may be added to the API.

Currently, we offer one market: **3-way**, which indicates the probability of each possible outcome — **home win**, **away win**, or **draw**.

<br />

### What are the valid outcomes for probabilities?

Here are the valid probability outcomes:

* `home_team_winner`
* `away_team_winner`

<br />

***

[Back to top ↑](#table-of-contents)

***

# Reference Data

<div id="past-season" />

## Past Season Data

<br />

### How do I access past seasons results and stats?

Use the [Competition Seasons](https://developer.sportradar.com/badminton/reference/badminton-competition-seasons) endpoint to locate the `season_id` for the season you want to access. Use that `season_id` to interrogate the various seasons endpoints.

<br />

***

<div id="standings" />

## Standings / Tournaments

<br />

### What are the possible values for `cup_round` - `state` in the Season Links endpoint?

Here are the valid `cup_round.state` values and their definitions. These can be leveraged to determine the status of a cup round.

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

With the type of "league" they will have a `sr:league` prefix. With the type of "cup" they will have a `sr:cup` prefix.

<br />
[Back to top &uarr;](#table-of-contents)

***

***

<br />

<Callout icon="🙋" theme="default">
  ### More questions?
  Reach out to **[mediasupport@sportradar.com](mailto:mediasupport@sportradar.com)** for further assistance.
</Callout>

<br /><br />