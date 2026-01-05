# Champions Circle - Draft Tournament Guide

This guide walks you through running a complete draft-style tournament using the Champions Circle cog.

---

## Overview

The tournament flow is designed for **minimum player effort**:

1. Players apply in Discord
2. Admins approve applications
3. Captains are set and the snake draft starts
4. Captains pick players one at a time in Discord
5. Players automatically see their team channels
6. Matches play out, scores reported via button

---

## Phase 1: Initial Setup

### Step 1: Configure the Tournament

```
!ccsetup
```

This opens a configuration flow:
- Tournament title and description
- Tournament date/time
- Application duration (days)
- Game mode (1v1, 2v2, 3v3, 4v4)
- Number of teams (e.g., 8 for an 8-team tournament)

**What gets created:**
- `#tournament-applications` forum channel
- `#tournament-announcements` text channel
- "Champions" role (for approved applicants)
- "Team Captain" role
- Team 1, Team 2, Team 3... roles (hidden until draft)
- Team voice and text channels (private, per-team)

### Step 2: Configure Challonge

Get your Challonge API key from: https://challonge.com/settings/developer

```
!ccchallonge key YOUR_API_KEY_HERE
```

Create a tournament on Challonge (empty, no participants yet), then link it:

```
!ccchallonge tournament your-tournament-slug
```

**This automatically:**
- Creates "Team 1", "Team 2", etc. as participants on Challonge
- Maps them to Discord team roles
- Sets up score reporting

If teams weren't created (API key set after tournament), run:
```
!ccchallonge createteams
```

Optional: set or refresh the bracket URL for embeds:
```
!ccchallonge set https://challonge.com/your-bracket
!ccchallonge refresh
```

---

## Phase 2: Applications

### Open Applications

```
!ccstart
```

This posts an embed with an **Apply** button. Players click to apply.

### Review Applications

Applications create threads in the forum. Use the buttons to:
- **Approve** - Player gets "Champions" role, enters draft pool
- **Deny** - Player notified via DM

### View Applicant Status

```
!ccapplicants
```

Shows counts of active, approved, denied, and cancelled applications.

---

## Phase 3: The Draft

### Before the Draft

Set captains (one per team):

```
!ccdraft captains @Captain1 @Captain2 @Captain3 ...
```

Or set captains individually:

```
!ccdraft captain 1 @Captain1
```

Check your draft pool:

```
!ccdraft pool
```

Shows all approved players who haven't been drafted yet, with their rank.

Start the draft (snake order):

```
!ccdraft start
```

Check current status:

```
!ccdraft status
```

### During the Draft (Snake Order)

Captains pick **one player at a time** when their team is on the clock:

```
!ccdraft pick @PlayerA
```

Only the current team captain can pick (admins can override).

**Undo the last pick (if mistake):**
```
!ccdraft undo
```

**Admin override (manual assign/remove):**
```
!ccdraft assign 1 @PlayerA
!ccdraft remove 1 @PlayerA
```

### After the Draft

Lock the draft to prevent accidental changes:

```
!ccdraft lock
```

To make changes later:
```
!ccdraft unlock
```

---

## Phase 4: Tournament Play

### Match Reporting

Each team's private text channel has a "Report Score" button.

**Using the button:**
1. Team captain/member clicks "Report Score"
2. If multiple open matches, select which match
3. Enter score (e.g., "3-1")
4. Score submitted to Challonge

**Using commands:**
```
!ccscore report <match_id> <score>
```

Example: `!ccscore report 12345 3-1`

### Viewing Matches

```
!ccchallonge matches
```

Shows current open and pending matches.

---

## Phase 5: End Tournament

When the tournament is complete:

```
!ccend
```

This:
- Archives all application threads
- Removes team roles from everyone
- Clears all channels
- Resets the cog for the next tournament

---

## Quick Command Reference

### Setup
| Command | Description |
|---------|-------------|
| `!ccsetup` | Configure tournament settings |
| `!ccstart` | Open applications |
| `!ccend` | End tournament and cleanup |

### Challonge
| Command | Description |
|---------|-------------|
| `!ccchallonge key <token>` | Set API key |
| `!ccchallonge tournament <slug>` | Set tournament slug |
| `!ccchallonge set <bracket_url>` | Set bracket URL |
| `!ccchallonge refresh` | Refresh bracket URL |
| `!ccchallonge createteams` | Create team participants |
| `!ccchallonge info` | Show tournament info |
| `!ccchallonge participants` | List Challonge participants |
| `!ccchallonge matches` | Show matches |
| `!ccchallonge sync` | Sync rosters to Challonge |
| `!ccchallonge syncroles` | Re-sync team roles |
| `!ccchallonge purgeall` | Remove all Challonge participants |

### Draft
| Command | Description |
|---------|-------------|
| `!ccdraft pool` | Show undrafted players |
| `!ccdraft captains @p1 @p2 ...` | Set captains for all teams |
| `!ccdraft captain 1 @player` | Set team captain |
| `!ccdraft start` | Start the snake draft |
| `!ccdraft pick @player` | Pick player when on the clock |
| `!ccdraft status` | Show draft status |
| `!ccdraft undo` | Undo last pick |
| `!ccdraft reset` | Reset pick order (keeps assignments) |
| `!ccdraft show` | Show all team rosters |
| `!ccdraft show 1` | Show specific team roster |
| `!ccdraft assign 1 @p1` | Admin override assign |
| `!ccdraft remove 1 @player` | Admin override remove |
| `!ccdraft lock` | Lock draft |
| `!ccdraft unlock` | Unlock draft |
| `!ccdraft clear` | Clear all assignments |
| `!ccdraft clear 1` | Clear specific team |

### Score Reporting
| Command | Description |
|---------|-------------|
| `!ccscore panel` | Post score button in team channel |
| `!ccscore report <id> <score>` | Submit score directly |

---

## Example Tournament Flow

```
Day 1: Setup
-----------
!ccsetup
  Configure: "Season 5 Championship", 3v3, 8 teams, 7 day applications

!ccchallonge key abc123xyz...
!ccchallonge tournament season-5-championship
  "Created 8 teams on Challonge. Ready for draft!"

!ccstart
  Applications open, embed posted

Days 2-7: Applications
----------------------
Players apply via button
Admins approve/deny in forum threads

Day 8: Draft Night (on stream)
------------------------------
!ccdraft pool
  Shows 24 approved players

!ccdraft captains @Captain1 @Captain2 @Captain3 @Captain4
!ccdraft start

Captain 1 picks: "I'll take PlayerA"
  !ccdraft pick @PlayerA

Captain 2 picks: "PlayerB for Team 2"
  !ccdraft pick @PlayerB

...continues until all teams filled...

!ccdraft show
  Shows all 8 teams with 3 players each

!ccdraft lock

Day 9+: Matches
---------------
Matches play out
Players use "Report Score" button in team channels
Bracket progresses on Challonge

Finals Day:
-----------
Grand finals happen
Winner crowned

!ccend
  Tournament archived, ready for next season
```

---

## Troubleshooting

### "No team roles found"
Run `!ccstart` to create team channels and roles.

### Teams not showing on Challonge
Run `!ccchallonge createteams` to manually create them.

### Player can't see team channel
Check they were properly assigned with `!ccdraft show <team#>`.
Re-sync roles with `!ccchallonge syncroles`.

### Wrong player on team
If the draft is in progress, use `!ccdraft undo` to roll back the last pick.

```
!ccdraft unlock
!ccdraft remove <team#> @wrongplayer
!ccdraft assign <team#> @rightplayer
!ccdraft lock
```

### Score not submitting
- Check Challonge API key is set: `!ccchallonge key`
- Check tournament is linked: `!ccchallonge tournament`
- Check team mapping exists: `!ccchallonge participants`

---

## Architecture Notes

**Challonge Structure:**
- Each **team** is a Challonge participant (not individual players)
- The `misc` field stores comma-separated Discord user IDs
- Matches are between teams (Team 1 vs Team 4, etc.)

**Discord Structure:**
- Individual players have team roles (Team 1, Team 2, etc.)
- Team channels are locked to their respective role
- Captains have additional "Team Captain" role

**Data Flow:**
```
Application -> Approval -> Draft Pool -> Draft Assignment -> Team Role -> Channel Access
                           |
                           -> Challonge misc field updated
```
