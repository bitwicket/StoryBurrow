# Frequently Asked Questions

Common questions about installing and using **StoryBurrow v0.9.2**. For the initial setup, start with the [main README](../README.md). StoryBurrow is still pre-1.0 software, so behavior may change between releases.

## General

### What is StoryBurrow?

StoryBurrow is a self-hosted browser-based library and player for **Linux-compatible Ren'Py games**. The game runs on your Linux server and is streamed to a web browser using Selkies. You interact through the browser, while the game files and saves stay on your server.

### Does StoryBurrow come with games, or download games for me?

No. You supply games you are permitted to use. StoryBurrow is not a storefront or downloader. VNDB integration provides available metadata and cover artwork; it does not supply game files.

### Does the game run on my phone, tablet, or laptop?

The **game runs on your server**. Your browser receives the video/audio stream and sends back input. That means you don't need to convert each game to a Ren'Py web build or maintain separate installations on each client. Actual touch, keyboard, audio, and scaling behavior can vary by browser and game. Tested Devices: Windows Desktop with Firefox and Chrome, Rog Ally X, SteamDeck, and iPhone. (iPhone performs best using the `Add to Home Screen` webapp function, removing the browser toolbars that eat up space.)

### Can multiple people play different games at the same time?

Not currently. StoryBurrow supports **one active game session per instance**. Additional user profiles and simultaneous sessions aren't implemented in v0.9.2.

### Can I access StoryBurrow away from home?

StoryBurrow is intended for a **trusted local network**. Do not directly forward its web port to the public internet. If you need remote access, use a properly secured VPN or equivalent private-network solution; internet-facing hardening is not a current feature.

## Installation and access

### What do I need to run StoryBurrow?

A Linux host with Docker Engine and Docker Compose, adequate storage for games and temporary updates, and compatible graphics/streaming support. Each imported game needs a Linux Ren'Py `.sh` launcher. See the [requirements and installation instructions](../README.md#requirements).

### Is there a Docker Hub image?

Yes! It is located [here](https://hub.docker.com/r/bitwicket/storyburrow) and referenced in the default compose.

### How do I create my first administrator account?

After starting the container, run this on the Docker host:

```bash
docker exec -it -u ubuntu storyburrow /opt/storyburrow-venv/bin/python /app/authctl.py init admin
```

You'll be prompted for a password. Don't put passwords in the command line, screenshots, or Compose file. Account data is stored under `/data/auth`, so **persistently mount `/data`** to keep the account across container rebuilds.

### Which address and port should I open?

Use `https://<your-server-address>:<STORYBURROW_PORT>`; the usual web port is **2500**. Set `STORYBURROW_HOST` to the IP address or hostname clients actually use. The Selkies service uses port **6080 inside the container** and is proxied through Caddy; you do **not** need to publish port 6080.

### Why does my browser warn about the certificate?

StoryBurrow's local HTTPS setup may use a certificate that your browser doesn't already trust. On a trusted LAN, review and trust the certificate only if you recognize the server and expect the warning. Certificate handling differs among browsers and devices. Do not treat a certificate exception as a substitute for securing public access.

### Why does the web page load but the player fail to connect?

First check that you're using the same host and port configured in `.env`, particularly `STORYBURROW_HOST` and `STORYBURROW_PORT`. Selkies origin and frame restrictions depend on those values. Also check container logs and make sure the Caddy/streaming services are running. A different hostname, stale browser session, or inaccessible HTTPS endpoint can prevent player initialization.

### How can I see container logs?

From the folder containing your Compose file:

```bash
docker compose logs --tail=200 storyburrow
```

For live logging, use `docker compose logs -f storyburrow`. Remove credentials, tokens, private paths, and game content before posting logs to a public issue.

## GPU and performance

### Does StoryBurrow need a GPU?

The tested default configuration uses Linux GPU passthrough for Selkies. CPU/software rendering may be possible, but it has **not yet been validated as a general default**. Streaming quality and CPU usage depend on your host, driver, encoder availability, game, and network.

### How do I configure Intel Quick Sync or AMD graphics?

On Linux, Intel and AMD commonly expose DRM devices via `/dev/dri`. The Compose configuration maps that device directory and adds the container user to your host's render group using `RENDER_GID` from `.env`. Follow the instructions in `.env.example` to identify the correct render group on **your** system; don't copy a random numeric GID.

### What about NVIDIA?

NVIDIA requires compatible host drivers and NVIDIA Container Toolkit setup, plus the appropriate Docker GPU configuration. It does not use precisely the same setup as the default Intel/AMD `/dev/dri` configuration. Follow the GPU guidance in the repository Compose comments and verify that the selected Selkies image supports your setup.

### Will GPU passthrough guarantee hardware video encoding?

No. Passing a GPU into the container makes hardware resources available; hardware encoding still depends on supported drivers, codecs, and Selkies configuration. Check the actual player behavior and logs rather than assuming passthrough means encoding is hardware accelerated.

### Why does streaming feel slow or blurry?

Check the server's CPU/GPU load, wired versus Wi-Fi network quality, browser behavior, and configured display resolution. High resolution increases the work needed to render and stream the game. Try a lower resolution first, and confirm whether the intended video encoder is being used.

## Games, installation, and updates

### Which games are supported?

StoryBurrow targets Linux-compatible Ren'Py distributions with a `.sh` launcher. It **doesn't directly execute Windows-only `.exe` builds**. Some titles may still have special dependencies or unusual save behavior that require troubleshooting.

### Can I import a ZIP or a game folder?

Yes. The browser import workflow accepts a game ZIP or folder and inspects the installation before importing it. StoryBurrow does not include the game's files itself.

### Can I replace an installed game with a newer or older release?

Yes. The Update Game workflow replaces the build while preserving the installation's managed save identity. It verifies save-routing compatibility and creates a **permanent pre-update save snapshot**. Existing mods are not automatically migrated into the replacement build.

### Does StoryBurrow keep the old game build?

During an update, the prior build is temporarily held so an update failure can be rolled back. After a successful update, you can choose to **retain the previous build for 48 hours** or discard it to save space. Retained builds are managed from **Edit Game → Manage previous builds**.

### How do I roll back a game update?

If you kept the previous build and it hasn't been deleted or expired, open **Edit Game → Manage previous builds** and select the rollback action. This restores the previous game files and recorded version. **It does not automatically restore old save files.**

### Why are full-build backups temporary?

Game builds can occupy several gigabytes each. Retaining multiple releases indefinitely would rapidly consume storage. The optional 48-hour window is intended for quick rollback after a broken update, while small save snapshots can remain available until manually deleted.

### Do old build backups disappear exactly 48 hours later?

They are eligible for automatic expiration after 48 hours. Cleanup is triggered by application activity, so an idle server may not delete an expired backup at the precise minute it expires. You can delete retained builds early in the UI.

## Saves, snapshots, and recovery

### Where are saves stored?

Managed installations use **separate, installation-owned save namespaces under `/saves`**, outside the game build. Keep `/saves` persistently mounted. StoryBurrow also maintains save routing so the Ren'Py process uses the correct managed location.

### Will updating or downgrading a game change my saves?

No automatic save rollback occurs during a game-build update or rollback. StoryBurrow creates a pre-update snapshot, but **you decide whether and when to restore it**. A save written by a newer game build may not work with an older one, so restoration should be an explicit decision.

### What's the difference between a save snapshot and a recovery copy?

A **snapshot** is a longer-term checkpoint, commonly created before an update. It remains until you delete it. A **temporary recovery copy** is created around supported save changes, including variable edits, and expires after **24 hours**.

### Can I export, import, and restore a snapshot?

Yes. Go to **Save Manager → Snapshots**. You can export a snapshot as a ZIP, import a previously exported StoryBurrow snapshot ZIP, restore files from it, or delete it. **Importing a ZIP adds a snapshot; it doesn't overwrite your active saves.** Restoring is a separate action.

### If I delete a snapshot, can I recover it later?

Snapshot deletion is permanent in the application. Export a ZIP before deletion if you might want it later. An exported ZIP can be re-imported as a snapshot.

### Can I move a save between games?

Save Manager supports exporting/importing individual save files and copying them between managed installations. **Copying a file does not guarantee it will work** in another game or version; Ren'Py save formats and game-specific variables may differ.

### Can I edit values inside a save?

StoryBurrow has a searchable editor for supported simple Ren'Py save variables. It is **not** a general-purpose editor for arbitrary Python objects or every Ren'Py release. Edits have temporary recovery protection, but a structurally valid save can still fail to load or behave incorrectly in the game.

### Why did Ren'Py show a save-trust or security warning?

Some modified saves may trigger a Ren'Py trust prompt. Only accept it when you recognize and trust the save. The warning does not guarantee the file is compatible with the game.

### Why can't I manage saves while a game is running?

To reduce the risk of changing files while Ren'Py is writing or reading them, StoryBurrow blocks save-management operations during active playback. Stop the game before importing, restoring, copying, deleting, or editing saves.

## Removal, storage, and maintenance

### What's the difference between Uninstall and Forget completely?

- **Uninstall game** removes the game installation and catalog entry but **preserves managed saves**.
- **Forget game completely** permanently removes the game and its verified installation-owned managed saves, snapshots, recovery copies, and registered previous builds.

Neither option is intended to guess at or delete unregistered legacy backup folders or unrelated external save directories. Confirm the action carefully; complete removal is destructive.

### Can I reinstall a game after uninstalling it?

Yes, but don't assume a new installation will automatically reconnect to every retained or legacy save directory. Review save routing/adoption options and maintain an independent backup of anything important.

### What folders should I keep persistent?

At minimum, preserve the Compose-mounted **`/games`, `/saves`, `/data`, `/logs`, and `/certs`** paths as applicable to your deployment. `/data` includes application state and administrator credentials; `/saves` contains managed saves and snapshots. Keep the mounts consistent when replacing the image.

### Can I upgrade the Docker image without losing my library?

The application is designed to store user data on persistent mounts rather than inside the image. However, **pre-1.0 upgrades and migrations should be treated carefully**: back up your mounted data and configuration before upgrading, then check the release notes for changed requirements.

### What should I include in a bug report?

Include your StoryBurrow version, host OS, relevant GPU/driver setup, browser, a concise reproduction sequence, and **sanitized** container logs. Never publicly attach private saves, credentials, signing keys, personal certificates, or copyrighted game archives.

---

**Still have a question?** Check the [main README](../README.md) and the guides in [`docs/`](./), or open a GitHub issue!