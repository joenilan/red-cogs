# Red-DiscordBot Cogs

This repository contains custom cogs for Red-DiscordBot, including a ModApplications cog for managing Twitch/Discord moderator applications.

## Cogs Included

1. **ModApplications**
   - Handles moderator applications for Twitch, Discord, YouTube, TikTok, and Kick
   - Includes a questionnaire system with platform-specific questions
   - Allows multiple platform selection
   - Automatic channel and role setup
   - Approve/Deny system with notifications

2. **ChampionsCircle**
   - Tournament management system
   - Application handling with buttons
   - Role management for participants

3. **DayPass**
   - Temporary role access management
   - Customizable durations
   - Automatic role removal

4. **CustomEmbedDM**
   - Custom DM embed messaging
   - Configurable titles, descriptions, and colors
   - Guild-specific settings

5. **MMIdleAlpha**
   - Redeem MMIdle alpha access codes directly from Discord
   - Uses secure shared-secret auth against MMIdle redeem API
   - Includes admin configuration commands for API and onboarding links

## Installation

To list all available cogs in this repository:

```
[p]cog list dz-cogs
```

To install any of these cogs, use the following command in your Red-DiscordBot:

```
[p]repo add dz-cogs https://github.com/joenilan/red-cogs.git
[p]cog install dz-cogs <cog_name>
```

For example, to install the ModApplications cog:

```
[p]cog install dz-cogs modapplications
```

Then load the cog:

```
[p]load modapplications
```

## Updating Cogs

To update the repository and all installed cogs:

```
[p]repo update dz-cogs
```

To update a specific cog:

```
[p]cog update dz-cogs <cog_name>
```

For example, to update the ModApplications cog:

```
[p]cog update dz-cogs modapplications
```

Then reload the cog:

```
[p]reload modapplications
```

## Usage

### ModApplications Cog

#### Setup
1. Open applications and create necessary channels/roles:
   ```
   !modapp open
   ```

2. Close applications and remove channels/roles:
   ```
   !modapp close
   ```

#### Applying
1. Users can apply using:
   ```
   !apply
   ```

#### Reviewing Applications
1. Applications will appear in the mod-applications channel
2. Use the Approve/Deny buttons to process applications

### MMIdleAlpha Cog

#### Setup
1. Install and load:
   ```
   [p]cog install dz-cogs mmidlealpha
   [p]load mmidlealpha
   ```
2. Configure API bridge:
   ```
   [p]mmalpha setapi https://game.mmidle.com
   [p]mmalpha setredeempath /api/integrations/discord/redeem
   [p]mmalpha setstatuspath /api/integrations/discord/status
   [p]mmalpha setapplyurl https://game.mmidle.com/apply
   [p]mmalpha setredeemurl https://game.mmidle.com/redeem
   [p]mmalpha setsecret <IDLEMMO_DISCORD_REDEEM_SECRET>
   [p]mmalpha show
   ```

#### Player Commands
- `/mmidle` (alias: `/mmidlealpha`): Show MMIdle alpha command shortcuts
- `/redeem <code>`: Redeem alpha code against linked MMIdle account
- `/alphalink`: Show MMIdle apply/link/redeem URLs
- `/alphastatus`: Show linked/account alpha status

#### Staff Command
- `/alphadiag [user]`: Admin-only API health + integration diagnostic for a Discord user

## Configuration

Each cog uses Red's Config system for persistent storage. Configuration can be managed through commands or directly in the bot's data folder.

## Requirements

- Python 3.8+
- Red-DiscordBot 3.5.0+
- discord.py 2.0+

## Contributing

Contributions are welcome! Please follow these steps:

1. Fork the repository
2. Create a new branch for your feature/fix
3. Commit your changes
4. Submit a pull request

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Support

For support or questions, please open an issue on GitHub or contact the maintainer.

## Changelog

### v1.0.0
- Initial release of ModApplications cog
- Added multi-platform support
- Automated channel/role setup
- Approve/Deny system with notifications 
