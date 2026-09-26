<!-- Source URL: https://developer.sportradar.com/baseball/reference/global-baseball-player-summaries.md
Retrieved: 2026-09-26 -->

---
updatedAt: 2026-05-29T13:08:44.000Z
---

Fetch the complete documentation index at: https://developer.sportradar.com/baseball/llms.txt. Use this file to discover all available pages before exploring further. Append .md to any documentation page URL to get its markdown version.

# Player Summaries

**Global Baseball Player Summaries** provides game info, results, and statistics for the past 10 games in which a given player participated.

<Callout icon="⏱️" theme="default">
  ### Update Frequency

  300s Time To Live / Cache
</Callout>

***

## Data Points

<Accordion title="Category & Sport" icon="fa-duotone fa-thin fa-baseball">

| Attribute | Parent Element | Type | Description |
| --------- | -------------- | ---- | ----------- |
| `country_code` | `sport_event_context` - `category` | String | Country code for a competition category<br /><br />ex. `JPN` (Japan) |
| `id` | `sport_event_context` - `category` | String | Unique category ID for a competition<br /><br />ex. `sr:category:211` |
| `name` | `sport_event_context` - `category` | String | Name for a competition category<br /><br />ex. `Japan` |
| `id` | `competitor_profile` - `sport` | String | Unique sport ID for a competitor<br /><br />ex. `sr:sport:3` |
| `name` | `competitor_profile` - `sport` | String | Name for a competitor's sport<br /><br />ex. `Baseball` |

</Accordion>

<Accordion title="Competition" icon="fa-duotone fa-thin fa-baseball">

| Attribute | Parent Element | Type | Description |
| --------- | -------------- | ---- | ----------- |
| `gender` | `sport_event_context` - `competition` | String | Gender for a competition<br /><br />`men`, `women` |
| `id` | `sport_event_context` - `competition` | String | Unique ID for a competition<br /><br />ex. `sr:competition:1036` (NPB) |
| `name` | `sport_event_context` - `competition` | String | Name of a competition<br /><br />ex. `NPB`, `Italian Baseball League` |
| `parent_id` | `sport_event_context` - `competition` | String | Unique parent ID for a competition. Typically present for group competitions<br /><br />ex. `sr:competition:37221` used to group NCAA tournaments. |

</Accordion>

<Accordion title="Groups" icon="fa-duotone fa-thin fa-baseball">

| Attribute | Parent Element | Type | Description |
| --------- | -------------- | ---- | ----------- |
| `id` | `sport_event_context` - `groups` - `group` | String | Unique ID for a sport event's group<br /><br />ex. `sr:league:80925` (National League West) |
| `name` | `sport_event_context` - `groups` - `group` | String | Name of a sport event's group<br /><br />ex. `MLB 2024, National League West` |
| `group_name` | `sport_event_context` - `groups` - `group` | String | Abbreviated name of a sport event's group<br /><br />ex. `National League West` |

</Accordion>

<Accordion title="Rounds" icon="fa-duotone fa-thin fa-baseball">

| Attribute | Parent Element | Type | Description |
| --------- | -------------- | ---- | ----------- |
| `competition_sport_event_number` | `sport_event_context` - `round` | Integer | Sport event number within a competition. Used for competitions that have fixed match numbers. |
| `cup_round_id` | `sport_event_context` - `round` | String | Unique ID for a sport event's cup round<br /><br />ex. `sr:cup_round:1988995` |
| `cup_round_number_of_sport_events` | `sport_event_context` - `round` | Integer | Number of events in a sport event's round |
| `cup_round_sport_event_number` | `sport_event_context` - `round` | Integer | Number within the `cup_round` of a match ID. For example, 1st leg of 2 |
| `name` | `sport_event_context` - `round` | String | Name of a sport event's round<br /><br />ex. `quarterfinal` or `final` |
| `number` | `sport_event_context` - `round` | Integer | Number of a sport event's round |
| `other_sport_event_id` | `sport_event_context` - `round` | String | A linked sport event ID, typically in a two-legged tie<br /><br />ex. `sr:sport_event:47395897` |

</Accordion>

<Accordion title="Seasons" icon="fa-duotone fa-thin fa-baseball">

| Attribute | Parent Element | Type | Description |
| --------- | -------------- | ---- | ----------- |
| `competition_id` | `sport_event_context` - `season` | String | Unique ID for the competition a season belongs to<br /><br />ex. `sr:competition:1036` (NPB 2024) |
| `disabled` | `sport_event_context` - `season` | Boolean | Signifies a season has been disabled when `true` |
| `end_date` | `sport_event_context` - `season` | Date | End date of a season<br /><br />ex. `2024-11-03` |
| `id` | `sport_event_context` - `season` | String | Unique ID for a season<br /><br />ex. `sr:season:99979` (World Baseball Classic 2023) |
| `name` | `sport_event_context` - `season` | String | Name of a season<br /><br />ex. `NPB 2024` |
| `start_date` | `sport_event_context` - `season` | Date | Start date of a season<br /><br />ex. `2024-03-29` |
| `year` | `sport_event_context` - `season` | String | Year of a season<br /><br />ex. `23/24` or `2024` |

</Accordion>

<Accordion title="Stages" icon="fa-duotone fa-thin fa-baseball">

| Attribute   | Parent Element               | Type   | Description |
|------------|-----------------------------|--------|-------------|
| `end_date` | `sport_event_context` - `stage` | Date   | End date of a sport event's stage<br /><br />ex. `2024-10-01` |
| `order`    | `sport_event_context` - `stage` | Integer | Order of a stage within a season |
| `phase`    | `sport_event_context` - `stage` | String | Name of a sport event's stage<br /><br />ex. `regular season`, `conference`, `playoffs`<br /><br />See the [enum_phase](https://api.sportradar.com/baseball/trial/v2/openapi/swagger/index.html) entry in our Global Baseball OpenAPI spec for a complete list of valid `phase` enum values |
| `start_date` | `sport_event_context` - `stage` | Date   | Start date of a sport event's stage<br /><br />ex. `2024-03-20` |
| `type`     | `sport_event_context` - `stage` | String | Type of a sport event's stage<br /><br />`cup`, `league` |
| `year`     | `sport_event_context` - `stage` | String | Year of a sport event's stage<br /><br />ex. `23/24` or `2024` |

</Accordion>

<Accordion title="Competitor" icon="fa-duotone fa-thin fa-baseball">

| Attribute | Parent Element | Type | Description |
| --------- | -------------- | ---- | ----------- |
| `abbreviation` | `competitor` | String | Abbreviation for a competitor name<br /><br />ex. `LAD` (Los Angeles Dodgers) |
| `age_group` | `competitor` | String | Age group of a competitor, when applicable<br /><br />ex. `U23` |
| `country` | `competitor` | String | Country of a competitor<br /><br />ex. `Japan` |
| `country_code` | `competitor` | String | Country code of a competitor<br /><br />ex. `JPN` (Japan) |
| `gender` | `competitor` | String | Gender for a competitor<br /><br />`male`, `female` |
| `id` | `competitor` | String | Unique ID for a competitor<br /><br />ex. `sr:competitor:3638` (Los Angeles Dodgers) |
| `name` | `competitor` | String | Name for a competitor<br /><br />ex. `Los Angeles Dodgers` |
| `qualifier` | `competitors` - `competitor` | String | Designation of a competitor for a sport event<br /><br />`home`, `away` |
| `virtual` | `competitor` | Boolean | Signifies a competitor is a virtual team when `true`. Used for placeholder teams in TBD vs TBD matchups. |

</Accordion>

<Accordion title="Coverage Properties" icon="fa-duotone fa-thin fa-baseball">

| Attribute | Parent Element | Type | Description |
| --------- | -------------- | ---- | ----------- |
| `type` | `sport_event` - `coverage` | String | Type of coverage for a sport event<br /><br />`sport_event`, `competition` |
| `basic_play_by_play` | `sport_event` - `coverage` - `sport_event_properties` | Boolean | Signifies basic play-by-play of a sport event is available when `true` |
| `scores` | `sport_event` - `coverage` - `sport_event_properties` | Strings | Indicates when sport event scores are made available<br /><br />`live`, `post` (Postgame) |

</Accordion>

<Accordion title="Sport Event" icon="fa-duotone fa-thin fa-baseball">

| Attribute | Parent Element | Type | Description |
| --------- | -------------- | ---- | ----------- |
| `neutral` | `sport_event_conditions` - `ground` | Boolean | Signifies a neutral ground sport event when `true` |
| `id` | `sport_event` | String | Unique ID of a sport event<br /><br />ex. `sr:sport_event:49796317` |
| `replaced_by` | `sport_event` | String | An alternative sport event ID if the match is postponed and played at a later date<br /><br />ex. `sr:sport_event:47395897` |
| `resume_time` | `sport_event` | Date | An updated timestamp if there is a delay at the start or interruption during a match<br /><br />ex. `2024-03-26T20:00:00+00:00` |
| `start_time` | `sport_event` | Date | Start time of a sport event<br /><br />ex. `2024-03-26T20:00:00+00:00` |
| `start_time_confirmed` | `sport_event` | Boolean | Signifies the start time of a sport event is confirmed when `true` |

</Accordion>

<Accordion title="Period Scores" icon="fa-duotone fa-thin fa-baseball">

| Attribute | Parent Element | Type | Description |
| --------- | -------------- | ---- | ----------- |
| `away_score` | `sport_event_status` - `period_scores` - `period_score` | Integer | Away team inning score |
| `home_score` | `sport_event_status` - `period_scores` - `period_score` | Integer | Home team inning score |
| `number` | `sport_event_status` - `period_scores` - `period_score` | Integer | Inning number |
| `type` | `sport_event_status` - `period_scores` - `period_score` | String | Inning type<br /><br />`started`, `overtime`, `overtime_break`, `break`, `interrupted`, `1st_inning`, `2nd_inning`, `3rd_inning`, `4th_inning`, `5th_inning`, `6th_inning`, `7th_inning`, `8th_inning`, `9th_inning`, `regular_period` |

</Accordion>

<Accordion title="Sport Event Status" icon="fa-duotone fa-thin fa-baseball">

| Attribute               | Parent Element       | Type     | Description |
|------------------------|--------------------|---------|-------------|
| `away_score`           | `sport_event_status` | Integer | Total score for the away team in the game |
| `away_normaltime_score`| `sport_event_status` | Integer | Score for the away team in normal time (9 innings) |
| `home_score`           | `sport_event_status` | Integer | Total score for the home team in the game |
| `home_normaltime_score`| `sport_event_status` | Integer | Score for the home team in normal time (9 innings) |
| `decided_inning`       | `sport_event_status` | Integer | Final inning number of a game |
| `match_status`         | `sport_event_status` | String  | Status within a game (sport event). Provides more detail on the state of a sport event when live than `status`<br /><br />ex. `not_started`, `start_delayed`, `1st_inning`, `postponed`<br /><br />See our [FAQ](https://developer.sportradar.com/baseball/reference/global-baseball-faq#sport-event-statuses) for a complete list of statuses and their definitions. |
| `scout_abandoned`     | `sport_event_status` | Boolean | Signifies a game was abandoned by a scout when `true` |
| `status`               | `sport_event_status` | String  | Status of a sport event<br /><br />ex. `not_started`, `live`, `ended`, `closed`<br /><br />See our [FAQ](https://developer.sportradar.com/baseball/reference/global-baseball-faq#sport-event-statuses) for a complete list of statuses and their definitions. |
| `winner_id`            | `sport_event_status` | String  | Unique ID of the game winner<br /><br />ex. `sr:competitor:3638` |

</Accordion>

<Accordion title="Game Stats (Team)" icon="fa-duotone fa-thin fa-baseball">

| Attribute | Parent Element | Type | Description |
| --------- | -------------- | ---- | ----------- |
| `at_bats` | `competitor` - `statistics` | Integer | Number of at bats for a team in a game |
| `earned_runs` | `competitor` - `statistics` | Integer | Number of earned runs allowed for a team in a game |
| `errors` | `competitor` - `statistics` | Integer | Number of errors committed by a team in a game |
| `hits` | `competitor` - `statistics` | Integer | Number of hits for a team in a game |
| `hits_allowed` | `competitor` - `statistics` | Integer | Number of hits allowed for a team in a game |
| `home_runs` | `competitor` - `statistics` | Integer | Number of home runs for a team in a game |
| `innings_pitched` | `competitor` - `statistics` | Integer | Number of innings pitched for a team in a game |
| `runs` | `competitor` - `statistics` | Integer | Number of runs scored for a team in a game |
| `runs_batted_in` | `competitor` - `statistics` | Integer | Number of runs batted in (RBIs) for a team in a game |
| `saves` | `competitor` - `statistics` | Integer | Number of saves for a team in a game |
| `stolen_bases` | `competitor` - `statistics` | Integer | Number of stolen bases for a team in a game |
| `walks` | `competitor` - `statistics` | Integer | Number of walks for a team in a game |
| `walks_allowed` | `competitor` - `statistics` | Integer | Number of walks allowed for a team in a game |

</Accordion>

<Accordion title="Game Stats (Player)" icon="fa-duotone fa-thin fa-baseball">

| Attribute | Parent Element | Type | Description |
| --------- | -------------- | ---- | ----------- |
| `at_bats` | `player` - `statistics` | Integer | Number of at bats for a player in a game |
| `earned_runs` | `player` - `statistics` | Integer | Number of earned runs allowed for a player in a game |
| `errors` | `player` - `statistics` | Integer | Number of errors committed by a player in a game |
| `hits` | `player` - `statistics` | Integer | Number of hits for a player in a game |
| `hits_allowed` | `player` - `statistics` | Integer | Number of hits allowed for a player in a game |
| `home_runs` | `player` - `statistics` | Integer | Number of home runs for a player in a game |
| `innings_pitched` | `player` - `statistics` | Integer | Number of innings pitched for a player in a game |
| `runs` | `player` - `statistics` | Integer | Number of runs scored for a player in a game |
| `runs_batted_in` | `player` - `statistics` | Integer | Number of runs batted in (RBIs) for a player in a game |
| `saves` | `player` - `statistics` | Integer | Number of saves for a player in a game |
| `stolen_bases` | `player` - `statistics` | Integer | Number of stolen bases for a player in a game |
| `walks` | `player` - `statistics` | Integer | Number of walks for a player in a game |
| `walks_allowed` | `player` - `statistics` | Integer | Number of walks allowed for a player in a game |

</Accordion>

<Accordion title="Venue" icon="fa-duotone fa-thin fa-baseball">

| Attribute | Parent Element | Type | Description |
| --------- | -------------- | ---- | ----------- |
| `capacity` | `venue` | Integer | Capacity of a venue<br /><br />ex. `56000` |
| `changed` | `venue` | Boolean | Signifies a venue has been changed when `true` |
| `city_id` | `venue` | String | Unique ID of a city<br /><br />ex. `sr:city:59` |
| `city_name` | `venue` | String | City name of a venue<br /><br />ex. `Los Angeles` |
| `country_code` | `venue` | String | Country code of a venue<br /><br />ex. `USA` |
| `country_name` | `venue` | String | Country name of a venue<br /><br />ex. `USA` |
| `id` | `venue` | String | Unique ID of a venue<br /><br />ex. `sr:venue:8117` |
| `map_coordinates` | `venue` | String | Coordinates of a venue<br /><br />ex. `34.0745409,-118.2408881` |
| `name` | `venue` | String | Name of a venue<br /><br />ex. `Dodger Stadium` |
| `reduced_capacity` | `venue` | Boolean | Optional attribute signifying a venue has a restricted capacity when `true` |
| `reduced_capacity_max` | `venue` | Integer | Value of a venue's restricted capacity |
| `timezone` | `venue` | String | Timezone of a venue<br /><br />ex. `America/Los_Angeles` |

</Accordion>

# OpenAPI definition

```json
{
  "openapi": "3.1.0",
  "info": {
    "title": "sportradar-api",
    "version": "8"
  },
  "servers": [
    {
      "url": "https://api.sportradar.com/"
    }
  ],
  "components": {
    "securitySchemes": {
      "sec0": {
        "type": "apiKey",
        "in": "header",
        "name": "x-api-key"
      }
    }
  },
  "security": [
    {
      "sec0": []
    }
  ],
  "paths": {
    "/{sport}/{access_level}/v2/{language_code}/players/{player_id}/summaries.{format}": {
      "get": {
        "summary": "Player Summaries",
        "description": "**Global Baseball Player Summaries** provides game info, results, and statistics for the past 10 games in which a given player participated.",
        "operationId": "global-baseball-player-summaries",
        "parameters": [
          {
            "name": "sport",
            "in": "path",
            "description": "Sport type<br><br>`baseball`, `softball`",
            "schema": {
              "type": "string",
              "enum": [
                "baseball",
                "softball"
              ],
              "default": "baseball"
            },
            "required": true
          },
          {
            "name": "access_level",
            "in": "path",
            "description": "The access level of your API key<br><br>`trial`, `production`",
            "schema": {
              "type": "string",
              "enum": [
                "trial",
                "production"
              ],
              "default": "trial"
            },
            "required": true
          },
          {
            "name": "language_code",
            "in": "path",
            "description": "2-letter code for supported languages<br><br>`en` (English), `es` (Spanish), `ja` (Japanese), `ko` (Korean), `zh` (Chinese - simplified), `zht` (Chinese - traditional)<br><br>The above codes are the fully supported languages for this API. Click <span style=\"text-decoration:underline\">[here](https://api-docs.sportradar.us/misc/Sportradar_API_Languages.xlsx)</span> for valid language codes and a tabular list of available languages per competition.",
            "schema": {
              "type": "string",
              "enum": [
                "en",
                "es",
                "ja",
                "ko",
                "zh",
                "zht"
              ],
              "default": "en"
            },
            "required": true
          },
          {
            "name": "player_id",
            "in": "path",
            "description": "Unique Id of a player<br><br>**Example:** `sr:player:1088424`",
            "schema": {
              "type": "string",
              "default": "sr:player:1088424"
            },
            "required": true
          },
          {
            "name": "format",
            "in": "path",
            "description": "Format returned<br><br>`json`, `xml`",
            "schema": {
              "type": "string",
              "enum": [
                "json",
                "xml"
              ],
              "default": "json"
            },
            "required": true
          }
        ],
        "responses": {
          "200": {
            "description": "200",
            "content": {
              "application/json": {
                "examples": {
                  "Result": {
                    "value": {
                      "generated_at": "2026-03-05T17:07:31.134Z",
                      "summaries": [
                        {
                          "sport_event": {
                            "channels": [
                              {
                                "country": "string",
                                "country_code": "string",
                                "name": "string",
                                "url": "string"
                              }
                            ],
                            "competitors": [
                              {
                                "abbreviation": "string",
                                "age_group": "U23",
                                "country": "string",
                                "country_code": "string",
                                "gender": "string",
                                "id": "string",
                                "name": "string",
                                "players": [
                                  {
                                    "country_code": "string",
                                    "date_of_birth": "2026-03-05",
                                    "gender": "female",
                                    "height": 0,
                                    "id": "string",
                                    "jersey_number": 0,
                                    "loaned_to_competitor_id": "string",
                                    "name": "string",
                                    "nationality": "string",
                                    "nickname": "string",
                                    "type": "C",
                                    "weight": 0
                                  }
                                ],
                                "qualifier": "string",
                                "virtual": true
                              }
                            ],
                            "coverage": {
                              "competition_properties": {
                                "brackets": true,
                                "schedules": true,
                                "standings": "live",
                                "team_squads": true
                              },
                              "sport_event_properties": {
                                "basic_play_by_play": true,
                                "scores": "live"
                              },
                              "type": "sport_event"
                            },
                            "id": "sr:sport_event:9385584568056650444857713793",
                            "replaced_by": "sr:sport_event:9201011534665336772951708547797854129225215125",
                            "resume_time": "2026-03-05T17:07:31.134Z",
                            "sport_event_conditions": {
                              "attendance": {
                                "count": 0
                              },
                              "comment": {
                                "text": "string"
                              },
                              "ground": {
                                "neutral": true
                              },
                              "referees": [
                                {
                                  "country_code": "string",
                                  "id": "string",
                                  "name": "string",
                                  "nationality": "string",
                                  "type": "main_referee"
                                }
                              ],
                              "weather": {
                                "overall_conditions": "good",
                                "pitch_conditions": "good"
                              }
                            },
                            "sport_event_context": {
                              "category": {
                                "country_code": "string",
                                "id": "string",
                                "name": "string"
                              },
                              "competition": {
                                "alternative_name": "string",
                                "gender": "string",
                                "id": "string",
                                "name": "string",
                                "parent_id": "string",
                                "type": "string"
                              },
                              "groups": [
                                {
                                  "group_name": "string",
                                  "id": "string",
                                  "name": "string"
                                }
                              ],
                              "round": {
                                "competition_sport_event_number": 0,
                                "cup_round_id": "string",
                                "cup_round_number_of_sport_events": 0,
                                "cup_round_sport_event_number": 0,
                                "name": "string",
                                "number": 0,
                                "other_sport_event_id": "sr:sport_event:390002707950887632778966784112984184906137551889"
                              },
                              "season": {
                                "competition_id": "string",
                                "disabled": true,
                                "end_date": "2026-03-05",
                                "id": "string",
                                "name": "string",
                                "start_date": "2026-03-05",
                                "year": "string"
                              },
                              "sport": {
                                "id": "string",
                                "name": "string"
                              },
                              "stage": {
                                "end_date": "2026-03-05",
                                "order": 0,
                                "phase": "1st_part_of_season_1st_leg",
                                "start_date": "2026-03-05",
                                "type": "cup",
                                "year": "string"
                              }
                            },
                            "start_time": "2026-03-05T17:07:31.134Z",
                            "start_time_confirmed": true,
                            "venue": {
                              "capacity": 0,
                              "changed": true,
                              "city_name": "string",
                              "country_code": "string",
                              "country_name": "string",
                              "id": "string",
                              "map_coordinates": "string",
                              "name": "string",
                              "reduced_capacity": true,
                              "reduced_capacity_max": 0,
                              "timezone": "string"
                            }
                          },
                          "sport_event_status": {
                            "aggregate_away_score": 0,
                            "aggregate_home_score": 0,
                            "aggregate_winner_id": "sr:competitor:6269171188904815625256275191389318669294019292943547780390663675888172698649",
                            "away_normaltime_score": 0,
                            "away_overtime_score": 0,
                            "away_score": 0,
                            "decided_by_fed": true,
                            "decided_inning": 0,
                            "home_normaltime_score": 0,
                            "home_overtime_score": 0,
                            "home_score": 0,
                            "match_status": "not_started",
                            "match_tie": true,
                            "period_scores": [
                              {
                                "away_score": 0,
                                "home_score": 0,
                                "number": 0,
                                "type": "started"
                              }
                            ],
                            "scout_abandoned": true,
                            "status": "not_started",
                            "winner_id": "string"
                          },
                          "statistics": {
                            "totals": {
                              "competitors": [
                                {
                                  "abbreviation": "string",
                                  "age_group": "U23",
                                  "country": "string",
                                  "country_code": "string",
                                  "gender": "string",
                                  "id": "string",
                                  "name": "string",
                                  "players": [
                                    {
                                      "id": "string",
                                      "name": "string",
                                      "statistics": {
                                        "at_bats": 0,
                                        "earned_runs": 0,
                                        "errors": 0,
                                        "hits": 0,
                                        "hits_allowed": 0,
                                        "home_runs": 0,
                                        "innings_pitched": 0,
                                        "runs": 0,
                                        "runs_batted_in": 0,
                                        "saves": 0,
                                        "stolen_bases": 0,
                                        "strikeouts": 0,
                                        "walks": 0,
                                        "walks_allowed": 0
                                      },
                                      "type": "batter"
                                    }
                                  ],
                                  "qualifier": "string",
                                  "statistics": {
                                    "at_bats": 0,
                                    "earned_runs": 0,
                                    "errors": 0,
                                    "hits": 0,
                                    "hits_allowed": 0,
                                    "home_runs": 0,
                                    "innings_pitched": 0,
                                    "runs": 0,
                                    "runs_batted_in": 0,
                                    "saves": 0,
                                    "stolen_bases": 0,
                                    "strikeouts": 0,
                                    "walks": 0,
                                    "walks_allowed": 0
                                  },
                                  "virtual": true
                                }
                              ]
                            }
                          }
                        }
                      ]
                    }
                  }
                },
                "schema": {
                  "type": "object",
                  "properties": {
                    "generated_at": {
                      "type": "string",
                      "example": "2026-03-05T17:07:31.134Z"
                    },
                    "summaries": {
                      "type": "array",
                      "items": {
                        "type": "object",
                        "properties": {
                          "sport_event": {
                            "type": "object",
                            "properties": {
                              "channels": {
                                "type": "array",
                                "items": {
                                  "type": "object",
                                  "properties": {
                                    "country": {
                                      "type": "string",
                                      "example": "string"
                                    },
                                    "country_code": {
                                      "type": "string",
                                      "example": "string"
                                    },
                                    "name": {
                                      "type": "string",
                                      "example": "string"
                                    },
                                    "url": {
                                      "type": "string",
                                      "example": "string"
                                    }
                                  }
                                }
                              },
                              "competitors": {
                                "type": "array",
                                "items": {
                                  "type": "object",
                                  "properties": {
                                    "abbreviation": {
                                      "type": "string",
                                      "example": "string"
                                    },
                                    "age_group": {
                                      "type": "string",
                                      "example": "U23"
                                    },
                                    "country": {
                                      "type": "string",
                                      "example": "string"
                                    },
                                    "country_code": {
                                      "type": "string",
                                      "example": "string"
                                    },
                                    "gender": {
                                      "type": "string",
                                      "example": "string"
                                    },
                                    "id": {
                                      "type": "string",
                                      "example": "string"
                                    },
                                    "name": {
                                      "type": "string",
                                      "example": "string"
                                    },
                                    "players": {
                                      "type": "array",
                                      "items": {
                                        "type": "object",
                                        "properties": {
                                          "country_code": {
                                            "type": "string",
                                            "example": "string"
                                          },
                                          "date_of_birth": {
                                            "type": "string",
                                            "example": "2026-03-05"
                                          },
                                          "gender": {
                                            "type": "string",
                                            "example": "female"
                                          },
                                          "height": {
                                            "type": "integer",
                                            "example": 0,
                                            "default": 0
                                          },
                                          "id": {
                                            "type": "string",
                                            "example": "string"
                                          },
                                          "jersey_number": {
                                            "type": "integer",
                                            "example": 0,
                                            "default": 0
                                          },
                                          "loaned_to_competitor_id": {
                                            "type": "string",
                                            "example": "string"
                                          },
                                          "name": {
                                            "type": "string",
                                            "example": "string"
                                          },
                                          "nationality": {
                                            "type": "string",
                                            "example": "string"
                                          },
                                          "nickname": {
                                            "type": "string",
                                            "example": "string"
                                          },
                                          "type": {
                                            "type": "string",
                                            "example": "C"
                                          },
                                          "weight": {
                                            "type": "integer",
                                            "example": 0,
                                            "default": 0
                                          }
                                        }
                                      }
                                    },
                                    "qualifier": {
                                      "type": "string",
                                      "example": "string"
                                    },
                                    "virtual": {
                                      "type": "boolean",
                                      "example": true,
                                      "default": true
                                    }
                                  }
                                }
                              },
                              "coverage": {
                                "type": "object",
                                "properties": {
                                  "competition_properties": {
                                    "type": "object",
                                    "properties": {
                                      "brackets": {
                                        "type": "boolean",
                                        "example": true,
                                        "default": true
                                      },
                                      "schedules": {
                                        "type": "boolean",
                                        "example": true,
                                        "default": true
                                      },
                                      "standings": {
                                        "type": "string",
                                        "example": "live"
                                      },
                                      "team_squads": {
                                        "type": "boolean",
                                        "example": true,
                                        "default": true
                                      }
                                    }
                                  },
                                  "sport_event_properties": {
                                    "type": "object",
                                    "properties": {
                                      "basic_play_by_play": {
                                        "type": "boolean",
                                        "example": true,
                                        "default": true
                                      },
                                      "scores": {
                                        "type": "string",
                                        "example": "live"
                                      }
                                    }
                                  },
                                  "type": {
                                    "type": "string",
                                    "example": "sport_event"
                                  }
                                }
                              },
                              "id": {
                                "type": "string",
                                "example": "sr:sport_event:9385584568056650444857713793"
                              },
                              "replaced_by": {
                                "type": "string",
                                "example": "sr:sport_event:9201011534665336772951708547797854129225215125"
                              },
                              "resume_time": {
                                "type": "string",
                                "example": "2026-03-05T17:07:31.134Z"
                              },
                              "sport_event_conditions": {
                                "type": "object",
                                "properties": {
                                  "attendance": {
                                    "type": "object",
                                    "properties": {
                                      "count": {
                                        "type": "integer",
                                        "example": 0,
                                        "default": 0
                                      }
                                    }
                                  },
                                  "comment": {
                                    "type": "object",
                                    "properties": {
                                      "text": {
                                        "type": "string",
                                        "example": "string"
                                      }
                                    }
                                  },
                                  "ground": {
                                    "type": "object",
                                    "properties": {
                                      "neutral": {
                                        "type": "boolean",
                                        "example": true,
                                        "default": true
                                      }
                                    }
                                  },
                                  "referees": {
                                    "type": "array",
                                    "items": {
                                      "type": "object",
                                      "properties": {
                                        "country_code": {
                                          "type": "string",
                                          "example": "string"
                                        },
                                        "id": {
                                          "type": "string",
                                          "example": "string"
                                        },
                                        "name": {
                                          "type": "string",
                                          "example": "string"
                                        },
                                        "nationality": {
                                          "type": "string",
                                          "example": "string"
                                        },
                                        "type": {
                                          "type": "string",
                                          "example": "main_referee"
                                        }
                                      }
                                    }
                                  },
                                  "weather": {
                                    "type": "object",
                                    "properties": {
                                      "overall_conditions": {
                                        "type": "string",
                                        "example": "good"
                                      },
                                      "pitch_conditions": {
                                        "type": "string",
                                        "example": "good"
                                      }
                                    }
                                  }
                                }
                              },
                              "sport_event_context": {
                                "type": "object",
                                "properties": {
                                  "category": {
                                    "type": "object",
                                    "properties": {
                                      "country_code": {
                                        "type": "string",
                                        "example": "string"
                                      },
                                      "id": {
                                        "type": "string",
                                        "example": "string"
                                      },
                                      "name": {
                                        "type": "string",
                                        "example": "string"
                                      }
                                    }
                                  },
                                  "competition": {
                                    "type": "object",
                                    "properties": {
                                      "alternative_name": {
                                        "type": "string",
                                        "example": "string"
                                      },
                                      "gender": {
                                        "type": "string",
                                        "example": "string"
                                      },
                                      "id": {
                                        "type": "string",
                                        "example": "string"
                                      },
                                      "name": {
                                        "type": "string",
                                        "example": "string"
                                      },
                                      "parent_id": {
                                        "type": "string",
                                        "example": "string"
                                      },
                                      "type": {
                                        "type": "string",
                                        "example": "string"
                                      }
                                    }
                                  },
                                  "groups": {
                                    "type": "array",
                                    "items": {
                                      "type": "object",
                                      "properties": {
                                        "group_name": {
                                          "type": "string",
                                          "example": "string"
                                        },
                                        "id": {
                                          "type": "string",
                                          "example": "string"
                                        },
                                        "name": {
                                          "type": "string",
                                          "example": "string"
                                        }
                                      }
                                    }
                                  },
                                  "round": {
                                    "type": "object",
                                    "properties": {
                                      "competition_sport_event_number": {
                                        "type": "integer",
                                        "example": 0,
                                        "default": 0
                                      },
                                      "cup_round_id": {
                                        "type": "string",
                                        "example": "string"
                                      },
                                      "cup_round_number_of_sport_events": {
                                        "type": "integer",
                                        "example": 0,
                                        "default": 0
                                      },
                                      "cup_round_sport_event_number": {
                                        "type": "integer",
                                        "example": 0,
                                        "default": 0
                                      },
                                      "name": {
                                        "type": "string",
                                        "example": "string"
                                      },
                                      "number": {
                                        "type": "integer",
                                        "example": 0,
                                        "default": 0
                                      },
                                      "other_sport_event_id": {
                                        "type": "string",
                                        "example": "sr:sport_event:390002707950887632778966784112984184906137551889"
                                      }
                                    }
                                  },
                                  "season": {
                                    "type": "object",
                                    "properties": {
                                      "competition_id": {
                                        "type": "string",
                                        "example": "string"
                                      },
                                      "disabled": {
                                        "type": "boolean",
                                        "example": true,
                                        "default": true
                                      },
                                      "end_date": {
                                        "type": "string",
                                        "example": "2026-03-05"
                                      },
                                      "id": {
                                        "type": "string",
                                        "example": "string"
                                      },
                                      "name": {
                                        "type": "string",
                                        "example": "string"
                                      },
                                      "start_date": {
                                        "type": "string",
                                        "example": "2026-03-05"
                                      },
                                      "year": {
                                        "type": "string",
                                        "example": "string"
                                      }
                                    }
                                  },
                                  "sport": {
                                    "type": "object",
                                    "properties": {
                                      "id": {
                                        "type": "string",
                                        "example": "string"
                                      },
                                      "name": {
                                        "type": "string",
                                        "example": "string"
                                      }
                                    }
                                  },
                                  "stage": {
                                    "type": "object",
                                    "properties": {
                                      "end_date": {
                                        "type": "string",
                                        "example": "2026-03-05"
                                      },
                                      "order": {
                                        "type": "integer",
                                        "example": 0,
                                        "default": 0
                                      },
                                      "phase": {
                                        "type": "string",
                                        "example": "1st_part_of_season_1st_leg"
                                      },
                                      "start_date": {
                                        "type": "string",
                                        "example": "2026-03-05"
                                      },
                                      "type": {
                                        "type": "string",
                                        "example": "cup"
                                      },
                                      "year": {
                                        "type": "string",
                                        "example": "string"
                                      }
                                    }
                                  }
                                }
                              },
                              "start_time": {
                                "type": "string",
                                "example": "2026-03-05T17:07:31.134Z"
                              },
                              "start_time_confirmed": {
                                "type": "boolean",
                                "example": true,
                                "default": true
                              },
                              "venue": {
                                "type": "object",
                                "properties": {
                                  "capacity": {
                                    "type": "integer",
                                    "example": 0,
                                    "default": 0
                                  },
                                  "changed": {
                                    "type": "boolean",
                                    "example": true,
                                    "default": true
                                  },
                                  "city_name": {
                                    "type": "string",
                                    "example": "string"
                                  },
                                  "country_code": {
                                    "type": "string",
                                    "example": "string"
                                  },
                                  "country_name": {
                                    "type": "string",
                                    "example": "string"
                                  },
                                  "id": {
                                    "type": "string",
                                    "example": "string"
                                  },
                                  "map_coordinates": {
                                    "type": "string",
                                    "example": "string"
                                  },
                                  "name": {
                                    "type": "string",
                                    "example": "string"
                                  },
                                  "reduced_capacity": {
                                    "type": "boolean",
                                    "example": true,
                                    "default": true
                                  },
                                  "reduced_capacity_max": {
                                    "type": "integer",
                                    "example": 0,
                                    "default": 0
                                  },
                                  "timezone": {
                                    "type": "string",
                                    "example": "string"
                                  }
                                }
                              }
                            }
                          },
                          "sport_event_status": {
                            "type": "object",
                            "properties": {
                              "aggregate_away_score": {
                                "type": "integer",
                                "example": 0,
                                "default": 0
                              },
                              "aggregate_home_score": {
                                "type": "integer",
                                "example": 0,
                                "default": 0
                              },
                              "aggregate_winner_id": {
                                "type": "string",
                                "example": "sr:competitor:6269171188904815625256275191389318669294019292943547780390663675888172698649"
                              },
                              "away_normaltime_score": {
                                "type": "integer",
                                "example": 0,
                                "default": 0
                              },
                              "away_overtime_score": {
                                "type": "integer",
                                "example": 0,
                                "default": 0
                              },
                              "away_score": {
                                "type": "integer",
                                "example": 0,
                                "default": 0
                              },
                              "decided_by_fed": {
                                "type": "boolean",
                                "example": true,
                                "default": true
                              },
                              "decided_inning": {
                                "type": "integer",
                                "example": 0,
                                "default": 0
                              },
                              "home_normaltime_score": {
                                "type": "integer",
                                "example": 0,
                                "default": 0
                              },
                              "home_overtime_score": {
                                "type": "integer",
                                "example": 0,
                                "default": 0
                              },
                              "home_score": {
                                "type": "integer",
                                "example": 0,
                                "default": 0
                              },
                              "match_status": {
                                "type": "string",
                                "example": "not_started"
                              },
                              "match_tie": {
                                "type": "boolean",
                                "example": true,
                                "default": true
                              },
                              "period_scores": {
                                "type": "array",
                                "items": {
                                  "type": "object",
                                  "properties": {
                                    "away_score": {
                                      "type": "integer",
                                      "example": 0,
                                      "default": 0
                                    },
                                    "home_score": {
                                      "type": "integer",
                                      "example": 0,
                                      "default": 0
                                    },
                                    "number": {
                                      "type": "integer",
                                      "example": 0,
                                      "default": 0
                                    },
                                    "type": {
                                      "type": "string",
                                      "example": "started"
                                    }
                                  }
                                }
                              },
                              "scout_abandoned": {
                                "type": "boolean",
                                "example": true,
                                "default": true
                              },
                              "status": {
                                "type": "string",
                                "example": "not_started"
                              },
                              "winner_id": {
                                "type": "string",
                                "example": "string"
                              }
                            }
                          },
                          "statistics": {
                            "type": "object",
                            "properties": {
                              "totals": {
                                "type": "object",
                                "properties": {
                                  "competitors": {
                                    "type": "array",
                                    "items": {
                                      "type": "object",
                                      "properties": {
                                        "abbreviation": {
                                          "type": "string",
                                          "example": "string"
                                        },
                                        "age_group": {
                                          "type": "string",
                                          "example": "U23"
                                        },
                                        "country": {
                                          "type": "string",
                                          "example": "string"
                                        },
                                        "country_code": {
                                          "type": "string",
                                          "example": "string"
                                        },
                                        "gender": {
                                          "type": "string",
                                          "example": "string"
                                        },
                                        "id": {
                                          "type": "string",
                                          "example": "string"
                                        },
                                        "name": {
                                          "type": "string",
                                          "example": "string"
                                        },
                                        "players": {
                                          "type": "array",
                                          "items": {
                                            "type": "object",
                                            "properties": {
                                              "id": {
                                                "type": "string",
                                                "example": "string"
                                              },
                                              "name": {
                                                "type": "string",
                                                "example": "string"
                                              },
                                              "statistics": {
                                                "type": "object",
                                                "properties": {
                                                  "at_bats": {
                                                    "type": "integer",
                                                    "example": 0,
                                                    "default": 0
                                                  },
                                                  "earned_runs": {
                                                    "type": "integer",
                                                    "example": 0,
                                                    "default": 0
                                                  },
                                                  "errors": {
                                                    "type": "integer",
                                                    "example": 0,
                                                    "default": 0
                                                  },
                                                  "hits": {
                                                    "type": "integer",
                                                    "example": 0,
                                                    "default": 0
                                                  },
                                                  "hits_allowed": {
                                                    "type": "integer",
                                                    "example": 0,
                                                    "default": 0
                                                  },
                                                  "home_runs": {
                                                    "type": "integer",
                                                    "example": 0,
                                                    "default": 0
                                                  },
                                                  "innings_pitched": {
                                                    "type": "integer",
                                                    "example": 0,
                                                    "default": 0
                                                  },
                                                  "runs": {
                                                    "type": "integer",
                                                    "example": 0,
                                                    "default": 0
                                                  },
                                                  "runs_batted_in": {
                                                    "type": "integer",
                                                    "example": 0,
                                                    "default": 0
                                                  },
                                                  "saves": {
                                                    "type": "integer",
                                                    "example": 0,
                                                    "default": 0
                                                  },
                                                  "stolen_bases": {
                                                    "type": "integer",
                                                    "example": 0,
                                                    "default": 0
                                                  },
                                                  "strikeouts": {
                                                    "type": "integer",
                                                    "example": 0,
                                                    "default": 0
                                                  },
                                                  "walks": {
                                                    "type": "integer",
                                                    "example": 0,
                                                    "default": 0
                                                  },
                                                  "walks_allowed": {
                                                    "type": "integer",
                                                    "example": 0,
                                                    "default": 0
                                                  }
                                                }
                                              },
                                              "type": {
                                                "type": "string",
                                                "example": "batter"
                                              }
                                            }
                                          }
                                        },
                                        "qualifier": {
                                          "type": "string",
                                          "example": "string"
                                        },
                                        "statistics": {
                                          "type": "object",
                                          "properties": {
                                            "at_bats": {
                                              "type": "integer",
                                              "example": 0,
                                              "default": 0
                                            },
                                            "earned_runs": {
                                              "type": "integer",
                                              "example": 0,
                                              "default": 0
                                            },
                                            "errors": {
                                              "type": "integer",
                                              "example": 0,
                                              "default": 0
                                            },
                                            "hits": {
                                              "type": "integer",
                                              "example": 0,
                                              "default": 0
                                            },
                                            "hits_allowed": {
                                              "type": "integer",
                                              "example": 0,
                                              "default": 0
                                            },
                                            "home_runs": {
                                              "type": "integer",
                                              "example": 0,
                                              "default": 0
                                            },
                                            "innings_pitched": {
                                              "type": "integer",
                                              "example": 0,
                                              "default": 0
                                            },
                                            "runs": {
                                              "type": "integer",
                                              "example": 0,
                                              "default": 0
                                            },
                                            "runs_batted_in": {
                                              "type": "integer",
                                              "example": 0,
                                              "default": 0
                                            },
                                            "saves": {
                                              "type": "integer",
                                              "example": 0,
                                              "default": 0
                                            },
                                            "stolen_bases": {
                                              "type": "integer",
                                              "example": 0,
                                              "default": 0
                                            },
                                            "strikeouts": {
                                              "type": "integer",
                                              "example": 0,
                                              "default": 0
                                            },
                                            "walks": {
                                              "type": "integer",
                                              "example": 0,
                                              "default": 0
                                            },
                                            "walks_allowed": {
                                              "type": "integer",
                                              "example": 0,
                                              "default": 0
                                            }
                                          }
                                        },
                                        "virtual": {
                                          "type": "boolean",
                                          "example": true,
                                          "default": true
                                        }
                                      }
                                    }
                                  }
                                }
                              }
                            }
                          }
                        }
                      }
                    }
                  }
                }
              }
            }
          }
        }
      }
    }
  },
  "x-readme": {
    "headers": [],
    "explorer-enabled": true,
    "proxy-enabled": true
  },
  "x-readme-fauxas": true
}
```