<!-- Source URL: https://developer.sportradar.com/badminton/reference/badminton-competitor-summaries.md
Retrieved: 2026-09-26 -->

---
updatedAt: 2026-05-27T18:38:01.000Z
---

Fetch the complete documentation index at: https://developer.sportradar.com/badminton/llms.txt. Use this file to discover all available pages before exploring further. Append .md to any documentation page URL to get its markdown version.

# Competitor Summaries

Provides scheduling information for upcoming matches and results for the 30 most recent completed matches of a specified competitor.

<Callout icon="⏱️" theme="default">
  ### Update Frequency

  300s Time To Live / Cache
</Callout>

***

## Data Points

<Accordion title="Category & Sport" icon="fa-thin fa-shuttlecock">

| Attribute | Parent Element | Type | Description |
| --------- | -------------- | ---- | ----------- |
| `country_code` | `sport_event_context` - `category` | String | Country code for a sport event's category |
| `id` | `sport_event_context` - `category` | String | Unique category ID for a sport event<br /><br />ex. `sr:category:259` (International) |
| `name` | `sport_event_context` - `category` | String | Name for a sport event's category<br /><br />ex. `International` |
| `id` | `sport_event_context` - `sport` | String | Unique sport ID for a sport event<br /><br />ex. `sr:sport:31` |
| `name` | `sport_event_context` - `sport` | String | Name for a sport event's sport<br /><br />ex. `Badminton` |

</Accordion>

<Accordion title="Competition" icon="fa-thin fa-shuttlecock">

| Attribute | Parent Element | Type | Description |
| --------- | -------------- | ---- | ----------- |
| `gender` | `sport_event_context` - `competition` | String | Gender for a competition<br /><br />ex. `men`, `women`, `mixed` |
| `id` | `sport_event_context` - `competition` | String | Unique ID for a competition<br /><br />ex. `sr:competition:41728` (Ruichang China Masters, MS) |
| `name` | `sport_event_context` - `competition` | String | Name of a competition<br /><br />ex. `Ruichang China Masters, MS`, `Toyota Thailand Open, Doubles` |
| `parent_id` | `sport_event_context` - `competition` | String | Unique parent ID for a competition.<br /><br />ex. `sr:competition:41726` is used to group all Ruichang China Masters competitions. |
| `type` | `sport_event_context` - `competition` | String | Type of a competition<br /><br />ex. `singles`, `doubles`, `mixed_doubles` |

</Accordion>

<Accordion title="Groups" icon="fa-thin fa-shuttlecock">

| Attribute | Parent Element | Type | Description |
| --------- | -------------- | ---- | ----------- |
| `id` | `sport_event_context` - `groups` - `group` | String | Unique ID for a sport event's group<br /><br />ex. `sr:cup:161825` (French Open 2024, MD) |
| `name` | `sport_event_context` - `groups` - `group` | String | Name of a sport event's group<br /><br />ex. `French Open 2024, MD` |
| `group_name` | `sport_event_context` - `groups` - `group` | String | Abbreviated name of a sport event's group |

</Accordion>

<Accordion title="Rounds" icon="fa-thin fa-shuttlecock">

| Attribute | Parent Element | Type | Description |
| --------- | -------------- | ---- | ----------- |
| `competition_sport_event_number` | `sport_event_context` - `round` | Integer | Sport event number within a competition. Used for competitions that have fixed match numbers. |
| `cup_round_id` | `sport_event_context` - `round` | String | Unique ID for a sport event's cup round<br /><br />ex. `sr:cup_round:1751367` |
| `cup_round_number_of_sport_events` | `sport_event_context` - `round` | Integer | Number of events in a sport event's round |
| `cup_round_sport_event_number` | `sport_event_context` - `round` | Integer | Number within the `cup_round` of a match ID. For example, 1st leg of 2 |
| `name` | `sport_event_context` - `round` | String | Name of a match's round<br /><br />ex. `quarterfinal` or `final` |
| `number` | `sport_event_context` - `round` | Integer | Number of a match's round |
| `other_sport_event_id` | `sport_event_context` - `round` | String | A linked sport event ID, typically in a two-legged tie<br /><br />ex. `sr:sport_event:47395897` |

</Accordion>

<Accordion title="Season" icon="fa-thin fa-shuttlecock">

| Attribute | Parent Element | Type | Description |
| --------- | -------------- | ---- | ----------- |
| `competition_id` | `sport_event_context` - `season` | String | Unique ID for the competition a season belongs to<br /><br />ex. `sr:competition:559` (Olympic Tournament, Doubles) |
| `disabled` | `sport_event_context` - `season` | Boolean | Signifies a season has been disabled when `true` |
| `end_date` | `sport_event_context` - `season` | Date | End date of a season<br /><br />ex. `2025-07-13` |
| `id` | `season` | String | Unique ID for a season<br /><br />ex. `sr:season:119999` (Olympic Tournament, Doubles 2024) |
| `name` | `sport_event_context` - `season` | String | Name of a season<br /><br />ex. `Olympic Tournament, Doubles 2024` |
| `start_date` | `sport_event_context` - `season` | Date | Start date of a season<br /><br />ex. `2025-06-23` |
| `year` | `sport_event_context` - `season` | String | Year of a season<br /><br />ex. `24/25` or `2024` |

</Accordion>

<Accordion title="Stages" icon="fa-thin fa-shuttlecock">

| Attribute   | Parent Element | Type   | Description |
|------------|----------------|--------|-------------|
| `end_date` | `sport_event_context` - `stage` | Date    | End date of a sport event's stage<br /><br />ex. `2024-07-14` |
| `order`    | `sport_event_context` - `stage` | Integer | Order of a stage within a season |
| `phase`    | `sport_event_context` - `stage` | String  | Name of a sport event's stage<br /><br />ex. `ms` (men singles), `md` (men doubles), `ws` (women singles), `wd` (women doubles), `xd` (mixed doubles)<br /><br />See the <b>enum_phase</b> entry in our [Badminton OpenAPI](https://api.sportradar.com/badminton/trial/v2/openapi/swagger/index.html) spec for a complete list of valid `phase` enum values |
| `start_date` | `sport_event_context` - `stage` | Date    | Start date of a sport event's stage<br /><br />ex. `2024-06-24` |
| `type`     | `sport_event_context` - `stage` | String  | Type of a match's stage<br /><br />`cup`, `league` |
| `year`     | `sport_event_context` - `stage` | String  | Year of a match's stage<br /><br />ex. `23/24` or `2024` |


</Accordion>

<Accordion title="Competitor" icon="fa-thin fa-shuttlecock">

> Competitors can be a player or a doubles team

| Attribute | Parent Element | Type | Description |
| --------- | -------------- | ---- | ----------- |
| `abbreviation` | `competitors` - `competitor` | String | Abbreviation for a competitor name (singles or doubles)<br /><br />ex. `SHI` (Shi, Yu Qi), `LIW` (Liang W K / Wang C) |
| `country` | `competitors` - `competitor` | String | Country of a competitor<br /><br />ex. `China` |
| `country_code` | `competitors` - `competitor` | String | Country code of a competitor<br /><br />ex. `CHN` |
| `date_of_birth` | `competitor` - `competitor` | Date | Date of birth of a competitor<br /><br />ex. `1996-02-28` |
| `gender` | `competitors` - `competitor` | String | Gender for a competitor<br /><br />`male`, `female` |
| `id` | `competitors` - `competitor` | String | Unique ID for a competitor<br /><br />ex. `sr:competitor:164376` |
| `name` | `competitors` - `competitor` | String | Name for a competitor (singles or doubles)<br /><br />ex. `Shi, Yu Qi`, `Liang W K / Wang C` |
| `qualifier` | `competitors` - `competitor` | String | Designation of a competitor for a match<br /><br />`home`, `away` |
| `virtual` | `competitors` - `competitor` | Boolean | Signifies a competitor is virtual when `true`. Used for placeholder players/teams in TBD vs TBD matchups. |

</Accordion>

<Accordion title="Player" icon="fa-thin fa-shuttlecock">

> Competitors will appear under a `players` node when part of a doubles competition.

| Attribute | Parent Element | Type | Description |
| --------- | -------------- | ---- | ----------- |
| `abbreviation` | `competitor` - `players` - `player` | String | Abbreviation for a player name<br /><br />ex. `LIA` (Liang, Wei Keng) |
| `country` | `competitor` - `players` - `player` | String | Country of a competitor<br /><br />ex. `China` |
| `country_code` | `competitor` - `players` - `player` | String | Country code of a competitor<br /><br />ex. `CHN` |
| `date_of_birth` | `competitor` - `players` - `player` | Date | Date of birth of a player<br /><br />ex. `1994-07-05` |
| `id` | `competitor` - `players` - `player` | String | Unique ID for a competitor<br /><br />ex. `sr:competitor:569895` |
| `name` | `competitor` - `players` - `player` | String | Name for a competitor<br /><br />ex. `Liang, Wei Keng` |

</Accordion>

<Accordion title="Coverage Properties" icon="fa-thin fa-shuttlecock">

| Attribute | Parent Element | Type | Description |
| --------- | -------------- | ---- | ----------- |
| `live` | `sport_event` - `coverage` | String | Signifies a sport event is covered live when `true` |

</Accordion>

<Accordion title="Sport Event" icon="fa-thin fa-shuttlecock">

| Attribute | Parent Element | Type | Description |
| --------- | -------------- | ---- | ----------- |
| `id` | `sport_event` | String | Unique ID of a match<br /><br />ex. `sr:sport_event:50453019` |
| `replaced_by` | `sport_event` | String | An alternative sport event ID if the match is postponed and played at a later date<br /><br />ex. `sr:sport_event:51847153` |
| `resume_time` | `sport_event` | Date | An updated timestamp if there is a delay at the start or interruption during a match<br /><br />ex. `2024-03-26T20:00:00+00:00` |
| `start_time` | `sport_event` | Date | Start time of a match<br /><br />ex. `2024-03-26T20:00:00+00:00` |
| `start_time_confirmed` | `sport_event` | Boolean | Signifies the start time of a match is confirmed when `true` |

</Accordion>

<Accordion title="Sport Event Status" icon="fa-thin fa-shuttlecock">

| Attribute        | Parent Element          | Type    | Description |
|-----------------|------------------------|---------|-------------|
| `away_score`     | `sport_event_status`   | Integer | Total score for the away competitor in a match |
| `home_score`     | `sport_event_status`   | Integer | Total score for the home competitor in a match |
| `match_status`   | `sport_event_status`   | String  | Status within a match (sport event). Provides more detail on the state of a match when live than `status`<br /><br />ex. `not_started`, `interrupted`, `1st_set`, `walkover`<br /><br />See our [Badminton OAS](https://api.sportradar.com/badminton/trial/v2/openapi/swagger/index.html) for a complete list of statuses. |
| `scout_abandoned` | `sport_event_status` | Boolean | Signifies a match was abandoned by a scout when `true` |
| `status`         | `sport_event_status`   | String  | Status of a sport event<br /><br />ex. `not_started`, `live`, `ended`, `closed`<br /><br />See our [FAQ](https://developer.sportradar.com/badminton/reference/badminton-faq#sport-event-statuses) for a complete list of statuses and their definitions. |
| `winner_id`      | `sport_event_status`   | String  | Unique ID of the match winner<br /><br />ex. `sr:competitor:174268` |
| `winning_reason` | `sport_event_status`   | String  | Winning reason description for a match<br /><br />ex. `walkover` |


</Accordion>

<Accordion title="Period Scores" icon="fa-thin fa-shuttlecock">

| Attribute | Parent Element | Type | Description |
| --------- | -------------- | ---- | ----------- |
| `away_score` | `sport_event_status` - `period_scores` - `period_score` | Integer | Away competitor set score |
| `home_score` | `sport_event_status` - `period_scores` - `period_score` | Integer | Home competitor set score |
| `number` | `sport_event_status` - `period_scores` - `period_score` | Integer | Set number |
| `type` | `sport_event_status` - `period_scores` - `period_score` | String | Period type<br /><br /> ex. `set` |

</Accordion>

<Accordion title="Venue" icon="fa-thin fa-shuttlecock">

| Attribute | Parent Element | Type | Description |
| --------- | -------------- | ---- | ----------- |
| `capacity` | `venue` | Integer | Capacity of a venue<br /><br />ex. `14820` |
| `changed` | `venue` | Boolean | Signifies a venue has been changed when `true` |
| `city_id` | `venue` | String | Unique ID of a city<br /><br />ex. `sr:city:59` |
| `city_name` | `venue` | String | City name of a venue<br /><br />ex. `Jakarta` |
| `country_code` | `venue` | String | Country code of a venue<br /><br />ex. `IDN` (England) |
| `country_name` | `venue` | String | Country name of a venue<br /><br />ex. `Indonesia` |
| `id` | `venue` | String | Unique ID of a venue<br /><br />ex. `sr:venue:76057` |
| `map_coordinates` | `venue` | String | Coordinates of a venue<br /><br />ex. `54.707924, 25.236942` |
| `name` | `venue` | String | Name of a venue<br /><br />ex. `Court 1`, `Center Court`, or `Court 2` |
| `reduced_capacity` | `venue` | Boolean | Optional attribute signifying a venue has a restricted capacity when `true` |
| `reduced_capacity_max` | `venue` | Integer | Value of a venue's restricted capacity |
| `timezone` | `venue` | String | Timezone of a venue<br /><br />ex. `Asia/Jakarta` |

</Accordion>

# OpenAPI definition

```json
{
  "openapi": "3.1.0",
  "info": {
    "title": "badminton",
    "version": "2"
  },
  "servers": [
    {
      "url": "https://api.sportradar.com/badminton/"
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
    "/{access_level}/v2/{language_code}/competitors/{competitor_id}/summaries.{format}": {
      "get": {
        "summary": "Competitor Summaries",
        "description": "Provides scheduling information for upcoming matches and results for the 30 most recent completed matches of a specified competitor.",
        "operationId": "badminton-competitor-summaries",
        "parameters": [
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
            "description": "2-letter code for supported languages<br><br>`en` (English)<br><br>English is the only fully supported language for this API. Click <span style=\"text-decoration:underline\">[here](https://api-docs.sportradar.us/misc/Sportradar_API_Languages.xlsx)</span> for valid language codes and a tabular list of available languages per competition.",
            "schema": {
              "type": "string",
              "default": "en"
            },
            "required": true
          },
          {
            "name": "competitor_id",
            "in": "path",
            "description": "Unique Id of a competitor (player or doubles team)<br><br>**Example:** `sr:competitor:76587`",
            "schema": {
              "type": "string",
              "default": "sr:competitor:76587"
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
                    "value": "{\n  \"generated_at\": \"2023-05-08T18:47:31.930Z\",\n  \"summaries\": [\n    {\n      \"sport_event\": {\n        \"channels\": [\n          {\n            \"country\": \"string\",\n            \"country_code\": \"string\",\n            \"name\": \"string\",\n            \"url\": \"string\"\n          }\n        ],\n        \"competitors\": [\n          {\n            \"abbreviation\": \"string\",\n            \"country\": \"string\",\n            \"country_code\": \"string\",\n            \"date_of_birth\": \"string\",\n            \"id\": \"string\",\n            \"name\": \"string\",\n            \"players\": [\n              {\n                \"abbreviation\": \"string\",\n                \"country\": \"string\",\n                \"country_code\": \"string\",\n                \"date_of_birth\": \"2023-05-08\",\n                \"gender\": \"female\",\n                \"height\": 0,\n                \"id\": \"string\",\n                \"jersey_number\": 0,\n                \"name\": \"string\",\n                \"nationality\": \"string\",\n                \"nickname\": \"string\",\n                \"type\": \"d\",\n                \"weight\": 0\n              }\n            ],\n            \"qualifier\": \"string\",\n            \"virtual\": true\n          }\n        ],\n        \"coverage\": {\n          \"live\": true,\n          \"properties\": [\n            {\n              \"type\": \"string\",\n              \"value\": true\n            }\n          ]\n        },\n        \"id\": \"string\",\n        \"replaced_by\": \"string\",\n        \"resume_time\": \"2023-05-08T18:47:31.931Z\",\n        \"sport_event_conditions\": {\n          \"ground\": {\n            \"neutral\": true\n          }\n        },\n        \"sport_event_context\": {\n          \"category\": {\n            \"country_code\": \"string\",\n            \"id\": \"string\",\n            \"name\": \"string\"\n          },\n          \"competition\": {\n            \"gender\": \"string\",\n            \"id\": \"string\",\n            \"name\": \"string\",\n            \"parent_id\": \"string\",\n            \"type\": \"string\"\n          },\n          \"groups\": [\n            {\n              \"group_name\": \"string\",\n              \"id\": \"string\",\n              \"name\": \"string\"\n            }\n          ],\n          \"mode\": {\n            \"best_of\": 0\n          },\n          \"round\": {\n            \"cup_round_id\": \"string\",\n            \"cup_round_number_of_sport_events\": 0,\n            \"cup_round_sport_event_number\": 0,\n            \"name\": \"string\",\n            \"number\": 0\n          },\n          \"season\": {\n            \"competition_id\": \"string\",\n            \"disabled\": true,\n            \"end_date\": \"2023-05-08\",\n            \"id\": \"string\",\n            \"name\": \"string\",\n            \"start_date\": \"2023-05-08\",\n            \"year\": \"string\"\n          },\n          \"sport\": {\n            \"id\": \"string\",\n            \"name\": \"string\"\n          },\n          \"stage\": {\n            \"end_date\": \"2023-05-08\",\n            \"order\": 0,\n            \"phase\": \"1st_part_of_season_1st_leg\",\n            \"start_date\": \"2023-05-08\",\n            \"type\": \"cup\",\n            \"year\": \"string\"\n          }\n        },\n        \"start_time\": \"2023-05-08T18:47:31.931Z\",\n        \"start_time_confirmed\": true,\n        \"venue\": {\n          \"capacity\": 0,\n          \"changed\": true,\n          \"city_name\": \"string\",\n          \"country_code\": \"string\",\n          \"country_name\": \"string\",\n          \"id\": \"string\",\n          \"map_coordinates\": \"string\",\n          \"name\": \"string\",\n          \"reduced_capacity\": true,\n          \"reduced_capacity_max\": 0,\n          \"timezone\": \"string\"\n        }\n      },\n      \"sport_event_status\": {\n        \"aggregate_away_score\": 0,\n        \"aggregate_home_score\": 0,\n        \"aggregate_winner_id\": \"string\",\n        \"away_normaltime_score\": 0,\n        \"away_overtime_score\": 0,\n        \"away_score\": 0,\n        \"decided_by_fed\": true,\n        \"home_normaltime_score\": 0,\n        \"home_overtime_score\": 0,\n        \"home_score\": 0,\n        \"match_status\": \"not_started\",\n        \"match_tie\": true,\n        \"period_scores\": [\n          {\n            \"away_score\": 0,\n            \"home_score\": 0,\n            \"number\": 0,\n            \"type\": \"regular_period\"\n          }\n        ],\n        \"scout_abandoned\": true,\n        \"status\": \"not_started\",\n        \"winner_id\": \"string\",\n        \"winning_reason\": \"walkover\"\n      }\n    }\n  ]\n}"
                  }
                },
                "schema": {
                  "type": "object",
                  "properties": {
                    "generated_at": {
                      "type": "string",
                      "example": "2023-05-08T18:47:31.930Z"
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
                                    "country": {
                                      "type": "string",
                                      "example": "string"
                                    },
                                    "country_code": {
                                      "type": "string",
                                      "example": "string"
                                    },
                                    "date_of_birth": {
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
                                          "abbreviation": {
                                            "type": "string",
                                            "example": "string"
                                          },
                                          "country": {
                                            "type": "string",
                                            "example": "string"
                                          },
                                          "country_code": {
                                            "type": "string",
                                            "example": "string"
                                          },
                                          "date_of_birth": {
                                            "type": "string",
                                            "example": "2023-05-08"
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
                                            "example": "d"
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
                                  "live": {
                                    "type": "boolean",
                                    "example": true,
                                    "default": true
                                  },
                                  "properties": {
                                    "type": "array",
                                    "items": {
                                      "type": "object",
                                      "properties": {
                                        "type": {
                                          "type": "string",
                                          "example": "string"
                                        },
                                        "value": {
                                          "type": "boolean",
                                          "example": true,
                                          "default": true
                                        }
                                      }
                                    }
                                  }
                                }
                              },
                              "id": {
                                "type": "string",
                                "example": "string"
                              },
                              "replaced_by": {
                                "type": "string",
                                "example": "string"
                              },
                              "resume_time": {
                                "type": "string",
                                "example": "2023-05-08T18:47:31.931Z"
                              },
                              "sport_event_conditions": {
                                "type": "object",
                                "properties": {
                                  "ground": {
                                    "type": "object",
                                    "properties": {
                                      "neutral": {
                                        "type": "boolean",
                                        "example": true,
                                        "default": true
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
                                  "mode": {
                                    "type": "object",
                                    "properties": {
                                      "best_of": {
                                        "type": "integer",
                                        "example": 0,
                                        "default": 0
                                      }
                                    }
                                  },
                                  "round": {
                                    "type": "object",
                                    "properties": {
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
                                        "example": "2023-05-08"
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
                                        "example": "2023-05-08"
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
                                        "example": "2023-05-08"
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
                                        "example": "2023-05-08"
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
                                "example": "2023-05-08T18:47:31.931Z"
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
                                "example": "string"
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
                                      "example": "regular_period"
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
                              },
                              "winning_reason": {
                                "type": "string",
                                "example": "walkover"
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