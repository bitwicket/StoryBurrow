# StoryBurrow

**A self-hosted home for Ren’Py games.**

StoryBurrow runs Linux-compatible Ren’Py games on your own server and streams them to a browser on your local network. Your game installations, saves, and mods stay on the server instead of requiring separate game installations on each device.

I built StoryBurrow to make it easier to pick up a visual novel from a PC, handheld, or phone: keep the library in one place, open a browser, and play.

Developed by **Bitwicket** · **Current development milestone: v0.9.2 (pre-1.0)**

StoryBurrow does **not** include games or downloadable game content.


<!-- When screenshots are ready, uncomment:
![StoryBurrow library](docs/images/library.png)
-->

## What it does

StoryBurrow combines a Flask library interface, a virtual X11 display, and Selkies streaming. Games execute on the server; your browser displays video/audio and sends inputs back. No Ren’Py web conversion is required.

A managed installation has its own save namespace, separate from its game files. StoryBurrow can import installations, update them, inspect saves, preserve snapshots, and roll back retained game builds.

**Designed for a trusted home LAN.** StoryBurrow is not a storefront, game downloader, multi-tenant hosting service, or publicly hardened cloud platform. Do not forward its port directly to the internet; use a properly secured VPN for remote access.

## Features and roadmap

A ✓ means implemented in the current development build; — means not yet implemented.

| Feature | Status |
| --- | :---: |
| Browser library, playback, and persistent login | ✓ |
| Import Linux Ren’Py games from ZIP or folder | ✓ |
| VNDB metadata and cover artwork | ✓ |
| Edit game details | ✓ |
| Update or downgrade an installed game | ✓ |
| Keep managed save identity through game updates | ✓ |
| Automatic pre-update save snapshots | ✓ |
| Import, export, restore, and delete save snapshots | ✓ |
| Optional 48-hour previous-build retention | ✓ |
| Web-based rollback and early deletion of previous builds | ✓ |
| Uninstall while preserving saves | ✓ |
| Forget an installation and its owned managed data completely | ✓ |
| Managed, isolated saves per installation | ✓ |
| Adopt pre-existing games and legacy saves | ✓ |
| Browse save metadata, thumbnails, and game versions | ✓ |
| Import, export, copy, delete, and restore individual saves | ✓ |
| 24-hour recovery copies for supported save changes | ✓ |
| Search and edit supported simple Ren’Py save variables | ✓ |
| Command-line initial admin setup and emergency game controls | ✓ |
| Browser-based mod management | — |
| Browser based Gallery for screenshots | — |

## Requirements

- Linux host with Docker Engine and Docker Compose.
- Enough disk space for game installations, uploads, and any temporarily retained previous builds. Ren’Py games can be several gigabytes each.
- Linux-compatible Ren’Py distributions containing a `.sh` launcher. Windows-only executables are not run directly.
- A browser with access to the host over your LAN.
- Appropriate graphics device support/permissions for the GPU passthrough configuration selected in Compose.

Intel and AMD Linux GPU devices generally use `/dev/dri` plus a render group. NVIDIA requires its host driver and NVIDIA Container Toolkit configuration. Hardware access and video encoding capabilities depend on the host and Selkies; software-only operation has not yet been validated as a general default.

## Installation and first run

### 1. Prepare the configuration

#### Dockerhub image:
Download/Copy `docker-compose.yml` and `.env.example` as `.env` to your prefered compose location.

Set Variables in .env as described below.

#### Local Build
Clone the repository and create a local `.env` file from the provided example using `docker-compose.local.yml`
```bash
git clone https://github.com/Bitwicket/StoryBurrow.git
cd StoryBurrow
cp .env.example .env
```
Set Variables in .env as described below.


Edit `.env` to set `STORYBURROW_HOST` to the **LAN IP address or hostname users will actually visit**, `STORYBURROW_PORT`, timezone, display dimensions, and `RENDER_GID` for the default Intel/AMD passthrough. Follow the comments in `.env.example` to discover the correct render group ID; do not assume a particular numeric ID.

### 2. Start StoryBurrow

#### Dockerhub image

```bash
docker compose pull
docker compose up -d
```

#### Local Build

```bash
docker compose up -d --build
```

Only the StoryBurrow HTTPS web port (container `2500`) needs to be published. Selkies (`6080`) is proxied internally through Caddy; **do not publish `6080` separately**.

### 3. Create the administrator account

On a fresh installation, the browser will indicate that administrator setup is required. Run this command on the Docker host:

```bash
docker exec -it -u ubuntu storyburrow /opt/storyburrow-venv/bin/python /app/authctl.py init admin
```

This initializes the administrator account named `admin` and prompts for its password interactively. **Do not put a password on the command line or in Compose.** The account data is stored under `/data/auth` and should persist through container rebuilds as long as `/data` is mounted persistently.

### 4. Open the library

Visit `https://<your-server-address>:<STORYBURROW_PORT>` (normally port `2500`), accept or trust the local HTTPS certificate as appropriate for your own network, and sign in with the administrator credentials you just created.

Select **Add game** to import a Linux-compatible Ren’Py game. Only import games that you are permitted to use.

## Saves, snapshots, and rollback

**Active managed saves** live outside the installed game build. Updating a game creates a permanent pre-update save snapshot and does not automatically replace or roll back current saves.

| Recovery data | Retention |
| --- | --- |
| Pre-update and imported save snapshots | Until manually deleted |
| Temporary save recovery copies (including variable edits) | 24 hours |
| Full previous game build | Optional, up to 48 hours after a successful update |

During an update, you may choose whether to keep the old build for 48 hours. The previous build is held temporarily during the update transaction even if you opt out. Retained builds can be rolled back or deleted early from **Edit Game → Manage previous builds**. Backup expiration is handled by periodic application checks; an idle server may not remove a backup at the exact expiry second.

In **Save Manager → Snapshots**, you can export a snapshot ZIP, import it again later, restore snapshot files, or delete a snapshot permanently. Importing a snapshot **does not** automatically restore it to active saves.

In **Edit Game**, **Uninstall** removes the game files but keeps managed save data. **Forget game completely** removes the installation and verified installation-owned managed saves, snapshots, recovery data, and registered previous builds. Unregistered old development backups or external legacy save folders are not guessed at or deleted.

A rollback of a game build **does not roll back saves**. If a game version requires an older save, restore it separately from Save Manager. Changes to saves and save variables can cause incompatibilities, and modified saves may prompt Ren’Py to ask for device trust; approve such prompts only for saves you recognize.

Keep independent backups of saves and configuration that matter to you. Built-in recovery is not a substitute for your own backup plan.

## Limitations

- This is pre-1.0 software; deployment, upgrades, and recovery need careful testing.
- One active game session per StoryBurrow instance.
- Linux Ren’Py launchers only; no direct Windows executable support.
- Save-variable editing is limited to supported simple values, not arbitrary Ren’Py/Python objects.
- Save management is disabled while a game is running. Avoid launching from another browser during save write operations.
- Streaming and touch controls vary across games, browsers, and hardware.
- The built-in login and locally terminated HTTPS are intended for LAN deployments, **not direct internet exposure**.

## Documentation and project layout

- [Frequently Asked Questions](docs/FAQ.md)

| Path | Purpose |
| --- | --- |
| `app/` | Flask UI, library, game and save workflows |
| `app/templates/`, `app/static/` | Browser templates and assets |
| `services/` | s6-managed runtime services |
| `Dockerfile`, `docker-compose.yml` | Container build and deployment configuration |
| `Caddyfile` | Internal HTTPS reverse proxy |

The container includes `authctl.py` for administrator setup and `gamectl.py` for emergency game controls.

## Contributing and issues

Bug reports are welcome. Include the StoryBurrow version, relevant sanitized container logs, and reproduction steps. **Do not upload personal save files, secrets, credentials, signing keys, or copyrighted game archives to public issues.**

## Credits and licensing

Primary Testing Game: [Love By The Water](https://curebtw.itch.io/lbtw) from Cure and Pickle. A great game from great people that were gracious enough to let me use their game in screenshots. 

Developed by **Bitwicket** using Flask, Selkies, Caddy, and Ren’Py-compatible tooling. Each upstream project retains its own license and trademarks. StoryBurrow does not distribute third-party games or claim ownership of their content.

