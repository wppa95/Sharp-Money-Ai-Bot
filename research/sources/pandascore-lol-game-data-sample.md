<!-- Source URL: https://developers.pandascore.co/docs/data-sample-league-of-legends.md
Retrieved: 2026-09-26 -->

---
updatedAt: 2025-09-16T00:17:36.000Z
---

Fetch the complete documentation index at: https://developers.pandascore.co/llms.txt. Use this file to discover all available pages before exploring further. Append .md to any documentation page URL to get its markdown version.

# League of Legends

## 🎞️ Frames

League of Legends frames provide an overview of the game, team statistics and player statistics.

**Game overview**\
Each frame will indicate the `id`'s (*integer*) of the game, match and tournament, as well as the:

* `current_timestamp` - time elapsed since the beginning of the game, in seconds (*integer*).
* `paused` - whether the game is currently paused (*boolean*).

The fields `finished` (*boolean*) and `winner_id` (*integer*) are utilised to inform the user of the current game status. When the game ends, `finished` will return true, and the game `winner_id` will update to reflect the victorious team.

**Team statistics**\
The `red` and `blue` objects are utilised to differentiate between team sides in the game. Each side object will detail the team `id` (*integer*), `name` (*string*) and `acronym` (*string*), as well as their:

* `towers` - number of towers destroyed (*integer; maximum 13*).
* `gold` - gold accumulated by the team (*integer*).
* `kills` - enemy players killed (*integer*).
* `drakes` - Drakes killed (*integer*).
* `nashors`- Baron Nashors killed (*integer*).
* `herald` - Rift Heralds killed (*integer; maximum 1*).
* `inhibitors` - number of inhibitors destroyed (*integer*).
* `score` - games won by this team so far in the first-to/best-of match series (*integer*).
* `voidgrubs`- voidgrubs killed (*integer; maximum 6*).

**Player statistics**\
Team players are stored in the `players` object, with the player's positions/roles as the keys: `top`, `jun`, `mid`, `adc`, and `sup`. Each player object contains their `id` (*integer*) and `name` (*string*) for identification and their:

* `champion` - containing the `id`, `name`, and `image_url` of the player's selected champion (*object*).
* `cs` - player creep score, total count of enemy minions, aka "creeps", killed (*integer*).
* `level` - champion level (*integer*).
* `kills` - player kills (*integer*).
* `deaths` - player deaths (*integer*).
* `assists`- player assists (*integer*).
* `summoner_spells`- the `id`'s and `name`'s of the summoner spells equipped by the player (*array*).
* `items` - `id`'s, `name`'s and `type`'s (i.e. trinket or item) of player items. Empty item slots simply have the value `null`. (*array; maximum 7*).
* `hp` - champion remaining life, aka "hit points" (*integer*).

> 📘 Data points exclusive to the Pro Live Plan
>
> Player `items`, player `hp`, team `herald`, `voidgrubs` and `paused` are only available to customers on the Pro Live plan.

### Frame example

<Embed url="https://storage.googleapis.com/ppanda/schemas/lol-frame.json" title="null" favicon="https://storage.googleapis.com/favicon.ico" provider="storage.googleapis.com" href="https://storage.googleapis.com/ppanda/schemas/lol-frame.json" iframe="true" />

## :dragon: Events

The League of Legends event feed is a reflection of all of the player, epic monster and structural kills happening during the game.

All events have a unique identifier in the form of an `id` for the event, game and match (*integer*), as well as the following fields:

* `type` - event type. Always `kill_feed` in League of Legends (*string*).
* `ts` - event timestamp in milliseconds (*integer*).
* `ingame_timestamp` - current in-game timestamp, in seconds (*integer*).
* `payload` - event detailed information (*object*).

The payload object is structured as follows:

```json
{
  "type": string,
  "assists": array of objects,
  "killer": KillEntity,
  "killed": KillEntity
}
```

Each player who assisted in an event is displayed in the assists array. The payload `type` matches the `killed` entity. For example, if a Tower is destroyed, the payload `type` will be `tower`. If a player is killed by a "non-player", the kill is considered a suicide, and the payload `type` is `suicide`.

A KillEntity can be any of the following:

* `baron_nashor` - epic monster, Baron Nashor.
* `drake` - epic monster, Drake/Dragon.
* `inhibitor` - structure, can't be `killer`.
* `minion` - battling units, can't be `killed`.
* `other` - can't be `killed`.
* `player` - player in-game.
* `rift_herald` - epic monster, Rift Herald.
* `tower` - structure, tower/turret.

> 📘 Events are only available to customers on the Pro Live Plan

### Events examples

#### Rift herald

<Embed url="https://storage.googleapis.com/ppanda/schemas/lol-herald-kill-event.json" title="null" favicon="https://storage.googleapis.com/favicon.ico" provider="storage.googleapis.com" href="https://storage.googleapis.com/ppanda/schemas/lol-herald-kill-event.json" iframe="true" />

#### Inhibitor

<Embed url="https://storage.googleapis.com/ppanda/schemas/lol-inhibitor-kill-event.json" title="null" favicon="https://storage.googleapis.com/favicon.ico" provider="storage.googleapis.com" href="https://storage.googleapis.com/ppanda/schemas/lol-inhibitor-kill-event.json" iframe="true" />

#### Baron Nashor

<Embed url="https://storage.googleapis.com/ppanda/schemas/lol-nashor-kill-event.json" title="null" favicon="https://storage.googleapis.com/favicon.ico" provider="storage.googleapis.com" href="https://storage.googleapis.com/ppanda/schemas/lol-nashor-kill-event.json" iframe="true" />

#### Tower

<Embed url="https://storage.googleapis.com/ppanda/schemas/lol-tower-kill-event.json" title="null" favicon="https://storage.googleapis.com/favicon.ico" provider="storage.googleapis.com" href="https://storage.googleapis.com/ppanda/schemas/lol-tower-kill-event.json" iframe="true" />

#### Player

<Embed url="https://storage.googleapis.com/ppanda/schemas/lol-player-kill-event.json" title="null" favicon="https://storage.googleapis.com/favicon.ico" provider="storage.googleapis.com" href="https://storage.googleapis.com/ppanda/schemas/lol-player-kill-event.json" iframe="true" />

#### Drake

<Embed url="https://storage.googleapis.com/ppanda/schemas/lol-drake-kill-event.json" title="null" favicon="https://storage.googleapis.com/favicon.ico" provider="storage.googleapis.com" href="https://storage.googleapis.com/ppanda/schemas/lol-drake-kill-event.json" iframe="true" />

<br />