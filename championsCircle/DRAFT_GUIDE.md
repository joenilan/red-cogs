# Champions Circle - Draft Tournament Guide

This guide walks you through running a complete draft-style tournament using the Champions Circle cog.

---

## Overview

The tournament flow is designed for **minimum player effort**:

1. Players apply in Discord
2. Admins approve applications
3. Draft happens live on stream (captains pick players)
4. Admin mirrors picks in Discord with simple commands
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

Check your draft pool:

```
!ccdraft pool
```

Shows all approved players who haven't been drafted yet, with their rank.

Check team status:

```
!ccdraft show
```

Shows all teams and their current rosters.

### During the Draft (Live on Stream)

As captains make picks verbally on stream, mirror them in Discord:

**Assign players to a team:**
```
!ccdraft assign 1 @PlayerA @PlayerB @PlayerC
```

This:
- Adds those players to Team 1
- Gives them the "Team 1" role
- They can now see Team 1's private channels
- Updates Challonge with the roster info

**Remove a player from a team (if mistake):**
```
!ccdraft remove 1 @PlayerA
```

**Assign team captain:**
```
!ccdraft captain 1 @PlayerA
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
| `!ccchallonge tournament <slug>` | Link tournament (auto-creates teams) |
| `!ccchallonge createteams` | Manually create team participants |
| `!ccchallonge info` | Show tournament info |
| `!ccchallonge participants` | List Challonge participants |
| `!ccchallonge matches` | Show matches |
| `!ccchallonge syncroles` | Re-sync team roles |

### Draft
| Command | Description |
|---------|-------------|
| `!ccdraft pool` | Show undrafted players |
| `!ccdraft show` | Show all team rosters |
| `!ccdraft show 1` | Show specific team roster |
| `!ccdraft assign 1 @p1 @p2 @p3` | Assign players to team |
| `!ccdraft remove 1 @player` | Remove player from team |
| `!ccdraft captain 1 @player` | Set team captain |
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
─────────────
!ccsetup
  → Configure: "Season 5 Championship", 3v3, 8 teams, 7 day applications

!ccchallonge key abc123xyz...
!ccchallonge tournament season-5-championship
  → "Created 8 teams on Challonge. Ready for draft!"

!ccstart
  → Applications open, embed posted


Days 2-7: Applications
─────────────────────
Players apply via button
Admins approve/deny in forum threads


Day 8: Draft Night (on stream)
─────────────────────────────
!ccdraft pool
  → Shows 24 approved players

Draft starts...
Captain 1 picks: "I'll take PlayerA"
  !ccdraft assign 1 @PlayerA

Captain 2 picks: "PlayerB for Team 2"
  !ccdraft assign 2 @PlayerB

...continues until all teams filled...

!ccdraft show
  → Shows all 8 teams with 3 players each

!ccdraft lock


Day 9+: Matches
──────────────
Matches play out
Players use "Report Score" button in team channels
Bracket progresses on Challonge


Finals Day:
──────────
Grand finals happen
Winner crowned

!ccend
  → Tournament archived, ready for next season
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
Application → Approval → Draft Pool → Draft Assignment → Team Role → Channel Access
                                           ↓
                                    Challonge misc field updated
```
