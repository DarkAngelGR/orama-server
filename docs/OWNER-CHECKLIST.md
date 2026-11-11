# Owner checklist: Orama on the UGREEN DXP4800 Plus

This is the hands-on test of Release 1 on your own NAS. Nobody else can do it: it needs your
NAS, your VPN, your indexers, your Plex account and your TV.

**How to use it.** Do the steps in order. Each step says what to do, what you should see, and
what to write down if you see something else. Tick the box when what you see matches. If it does
not match, do **not** try to fix it yourself: write down what you saw (a phone photo of the
screen is perfect), then look at [If something fails](#if-something-fails) at the end.

**How long.** About two hours, most of it waiting for downloads.

**Never write down or photograph** your passwords, your VPN key or your indexer keys.

## Before you start

Have these ready:

- [ ] The NAS is on, updated, and you can open its normal web page.
- [ ] The **Docker** app is installed on the NAS from the UGREEN App Center and it is running.
- [ ] A computer on the same home network, to type one command into the NAS.
- [ ] Your phone, on the same Wi-Fi (not mobile data).
- [ ] Your Proton VPN account open in a browser, so you can download a WireGuard file with
      port forwarding turned on.
- [ ] The web address and key of each indexer you use, and the address, user name and password
      of your Usenet provider.
- [ ] Your Plex account name and password. Plex Pass is needed for one step (hardware
      transcoding in Plex); without it, skip only that part and write "no Plex Pass".
- [ ] The Samsung S95F, on, with the Plex app installed and signed in.
- [ ] The install command for the version you are testing. The developer gives you this
      line; it starts with `curl` and ends with `sudo bash`.

## The steps

### 1. Run the installer

- **Do:** On the NAS web page, turn on remote terminal access (Control Panel → Terminal → turn
  on SSH). On your computer open a terminal window and connect to the NAS with your NAS user
  name. Paste the install command and press Enter. Type your NAS password if it asks.
- **You should see**, in this order:
  - `Installing Orama … on ugos.`
  - A line starting `Media folder:` that names a folder on your storage volume for media and
    a folder for Orama itself.
  - `Downloading Orama …`, then `Starting Orama...`
  - A last line starting `✓ Orama is ready. Open http://` followed by a long link that ends in
    `/setup?code=` and some letters.
- **You should not see:** anything about installing Docker. On this NAS Orama must use the
  Docker app that is already there.
- **Write down if it differs:** the first line that starts with `✗`, word for word. Also write
  down the two folders from the `Media folder:` line, and the link's address part (the numbers
  between `http://` and `/setup`).
- [ ] Done, as described.

### 2. Open the setup link from your phone

- **Do:** Type the whole link from step 1 into your phone's browser (or send it to yourself and
  tap it).
- **You should see:** a page titled **Welcome to Orama** that asks you to create a password.
  The page fits the phone screen; you never have to scroll sideways.
- **Write down if it differs:** what the page says instead. If it says the link is not valid,
  note whether you opened the link once before.
- [ ] Done, as described.

### 3. Create your password

- **Do:** Type a password of at least 12 characters (a few words work well), then press
  **Create password**. Then, on your computer, try the same link from step 1 again.
- **You should see:** on the phone, the first setup page (**Media servers**). On the computer,
  the old link no longer offers to create a password.
- **Write down if it differs:** whether the old link still offered to create a password.
- [ ] Done, as described.

### 4. Answer the setup questions

- **Do:** Go through the pages on your phone. Use these answers:
  - **Media servers:** tick Plex and Jellyfin. Give Jellyfin an administrator name and a
    password of at least 12 characters.
  - **Folders:** leave as shown.
  - **Downloads and VPN:** tick Usenet and Torrents. Tick **Send torrent downloads through a
    VPN**, choose Proton VPN, paste the private key and the address from your WireGuard file,
    and tick **Use port forwarding**.
  - **Indexers:** add each indexer and press **Save and test indexer**. Add your Usenet
    provider and press **Save and test server**.
  - **Playback:** choose **Samsung S95F**.
  - **Subtitles:** first language Greek, second language English.
- **You should see:** each page moves on to the next without an error. Each indexer and the
  Usenet server shows that its test passed. The last page, **Review your setup**, lists your
  answers and shows no password or key in readable form.
- **Write down if it differs:** the page name and the message in red.
- [ ] Done, as described.

### 5. Install, and watch the list

- **Do:** On **Review your setup** press **Install**. Leave the phone on that page.
- **You should see:** a page titled **Starting your apps** with a list that grows by itself,
  one line at a time, each starting with `✓`. You do not have to reload. It can take ten
  minutes the first time. At the end it offers to continue, and shows a **Sign in with Plex**
  button and a line that Jellyfin is ready.
- **Write down if it differs:** any line that starts with `✗` (word for word), or if the list
  stops growing for more than ten minutes (note the last line).
- [ ] Done, as described.

### 6. VPN and port forwarding

- **Do:** In Orama open **Apps**. Check that every app shows as running, including the VPN
  entry. Open **qBittorrent** from that page and look at the small connection icon in the bar
  at the very bottom of its window.
- **You should see:** all apps running. The qBittorrent icon is a green globe or plug (not
  orange, not a red crossed circle). Hovering over it says the connection is working.
- **Then do:** in your Proton VPN account page, look at your active connections.
- **You should see:** one connection from the NAS.
- **Write down if it differs:** which app is not running, and the colour of the icon.
- [ ] Done, as described.

### 7. Indexers and Usenet really work

- **Do:** From **Apps**, open **Prowlarr**. Look at the list of indexers. Then open
  **SABnzbd** and look at the top of its page.
- **You should see:** in Prowlarr, every indexer you added, none marked with a red warning.
  In SABnzbd, no red or orange warning banner about the server or the login.
- **Write down if it differs:** the indexer's name and the warning text.
- [ ] Done, as described.

### 8. Plex sign-in and libraries

- **Do:** In Orama press **Sign in with Plex**. A Plex page opens; sign in with your Plex
  account and approve. Go back to Orama and press **Check approval** if it does not move on by
  itself. Then open **Plex** from **Apps**.
- **You should see:** Orama says the Plex sign-in is saved. In Plex, your NAS appears as a
  server you own, with exactly two libraries: **Movies** and **TV Shows**. Open the Plex app on
  your phone (still on home Wi-Fi): the same server is there.
- **Write down if it differs:** how many libraries there are and their names; whether the
  phone app finds the server.
- [ ] Done, as described.

### 9. Jellyfin

- **Do:** From **Apps**, open **Jellyfin**. Sign in with the administrator name and password
  you chose in step 4.
- **You should see:** you are signed in without any first-time setup questions. There are two
  libraries: **Movies** and **TV Shows**.
- **Write down if it differs:** whether Jellyfin asked setup questions, and the library names.
- [ ] Done, as described.

### 10. Hardware transcoding

Do this after step 11 has given you something to play, then come back and tick it.

- **Do (Plex, needs Plex Pass):** Play the film in the Plex app on your phone and set the
  quality to a low one (for example 720p). While it plays, open Plex on your computer →
  Settings → Dashboard.
- **You should see:** the "Now Playing" card says **Transcode (hw)** for the video.
- **Do (Jellyfin):** Play the same film in Jellyfin in a browser and set the quality to a low
  one. While it plays, open Jellyfin → Dashboard and click the playing session's info icon.
- **You should see:** it says the video is being transcoded, and the NAS does not slow to a
  crawl (the picture keeps up).
- **Write down if it differs:** the exact words on the card (for example "Transcode" without
  "(hw)"), and whether you had to switch hardware acceleration on yourself in either app's
  settings.
- [ ] Done, as described.

### 11. Ask for a film and an episode

- **Do:** In Orama, on **Home**, search for a film you do not have. Open it and request it.
  Then search for a show, open it, and request **one episode** only.
- **You should see:** each request gets its own page. The status moves by itself through
  **Searching**, **Downloading** with a percentage, **Importing**, and ends at **Ready**. The
  page updates without you reloading it. On **Home** both requests are listed.
- **Also check:** open **Sonarr** from **Apps** and look at the show: only the one episode you
  asked for is marked to be downloaded, not the whole season.
- **Write down if it differs:** the last status each request reached, any "waiting" reason
  shown, and how long you waited. If more than the one episode started downloading, write
  which ones.
- [ ] Done, as described.

### 12. Subtitles: Greek first, then English

- **Do:** Open each request page again once it shows **Ready**. Wait up to fifteen minutes.
- **You should see:** a subtitle line on the page. Best case: it says Greek subtitles are
  there. If there are no Greek subtitles for that title anywhere, it honestly says it has
  English and is still looking for Greek. It must never say subtitles are there when they are
  not.
- **Then do:** play the title in Plex and open the subtitle menu.
- **You should see:** the same languages Orama listed.
- **Write down if it differs:** what Orama's page said and what Plex's subtitle menu offered.
- [ ] Done, as described.

### 13. Play on the Samsung S95F

- **Do:** On the TV, open Plex and play the film, then the episode. While each one plays, open
  Plex on your computer → Settings → Dashboard.
- **You should see:** the picture and sound start within a few seconds. The "Now Playing" card
  says **Direct Play** (for both video and audio). Orama's request page for that title also
  said direct play on the Samsung S95F.
- **Write down if it differs:** the exact words on the card ("Transcode" or "Direct Stream",
  and whether for video or audio), and the words on Orama's request page.
- [ ] Done, as described.

### 14. Run the installer again: nothing changes

- **Do:** On the computer, connect to the NAS as in step 1 and paste the same install command
  again.
- **You should see:** it finishes much faster than the first time and ends with
  `✓ Orama is ready. Open http://…` — this time **without** `/setup?code=`. In Orama, **Apps**
  shows every app still running, your requests are still there and **Ready**, and nothing
  starts downloading again.
- **Write down if it differs:** any `✗` line; any app that restarted; any setting you had to
  enter again.
- [ ] Done, as described.

### 15. The Update Orama button

- **Do:** In Orama open **Settings**, find **Update Orama** and press the button. Wait; it can
  take a few minutes.
- **You should see one of two messages:**
  - "Orama is already on the newest version. Nothing was changed." — fine, nothing more to do.
  - "Update started. Orama restarts by itself if a newer version was downloaded — reload this
    page in a minute." — wait one minute, reload, and sign in again if asked.
- **Either way you should see afterwards:** **Apps** shows every app running, your requests are
  still there, and anything that was playing in Plex kept playing.
- **Try once more with the internet unplugged from the NAS for a moment** (or skip if that is
  awkward): the page should say Orama could not download the update and that nothing was
  changed.
- **Write down if it differs:** the message shown; whether Orama came back by itself; whether
  any app stopped.
- [ ] Done, as described.

### 16. A quick check with a screen reader

- **Do:** Turn on the screen reader on your phone (iPhone: Settings → Accessibility →
  VoiceOver; Android: Settings → Accessibility → TalkBack). In Orama open **Settings**, change
  the second subtitle language and save, so the list from step 5 runs again. Listen. When it
  has finished, change it back. Then, still in **Settings** under **Media servers**, untick
  Jellyfin and press **Save**: a page asks "Turn this off?". Listen to it, then go back
  **without** pressing "Yes, turn it off".
- **You should hear:** each new line of the list read out once as it appears, not the whole
  list again each time. Every button has a spoken name. The "Turn this off?" page starts by
  reading its question.
- **Write down if it differs:** what was read twice, what was silent, or which button had no
  name.
- [ ] Done, as described.

## If something fails

1. **Stop at that step.** Do not reinstall and do not change settings to work around it; the
   point is to see what a normal person would see.
2. **Write down:** the step number, what you did, what you saw (a photo of the screen is best),
   and the time.
3. **Collect what Orama recorded.** Connect to the NAS as in step 1 and run the line that fits:

   | What failed | Command |
   |---|---|
   | Anything on an Orama page | `sudo docker logs --tail 100 orama-server` |
   | Installing, starting or stopping apps | `sudo docker logs --tail 100 orama-server-agent` |
   | The Update Orama button | `sudo docker logs --tail 100 orama-updater` |
   | One app (use its name from the Apps page, in small letters) | `sudo docker logs --tail 100 orama-sonarr` |

   Orama hides passwords and keys in its own records. Still, read what you copied once and
   remove anything that looks like a password, a key or your home address before sharing it.
4. **Who to tell:** start a new session with the assistant you are working with (Claude, or
   whichever tool is on duty) and say: "Owner checklist, step N failed", then paste what you
   wrote down and the copied lines. The assistant records it in `docs/tasks/plan-5/T04.md`,
   fixes it with a test, and tells you which step to repeat.
5. **Is it safe to leave it running meanwhile?** Yes. A failed step does not put your files at
   risk; Orama never deletes media.

## For the developer

Technical detail behind the plain steps, and what each step is evidence for. Record results in
`docs/tasks/plan-5/T04.md`.

- **Getting the version under test onto the NAS.** The steps assume the images are downloadable
  (Plan 5 Task 5: after the reviews, `test` is merged, `publish` tags the images). While the
  GHCR packages are private the NAS needs `docker login ghcr.io` once with a read-only token, or
  the packages are made public first. The install command is the one at the top of
  `install.sh`.
- **Step 1** proves UGOS detection (`ID=ugos` in `/etc/os-release` → `/volume1/docker/orama`
  and `/volume1/data`), that UGOS's bundled Docker is used and none is installed, and that the
  UGOS-reserved ports (9999, 9443) are avoided. The address in the link is the LAN address the
  installer found from the default route. Graphics detection (`/dev/dri/renderD128` group id,
  passed to Plex/Jellyfin) is only visible in `hardware.json` and in step 10's result.
- **Step 2–3**: one-time setup code (single use; gone once a password exists). The Plan 3 notes
  ask what address Orama sees for a LAN phone behind Docker's port mapping (sessions are tied to
  the sign-in address): if step 2 or a later page signs the phone out unexpectedly, capture
  `docker logs orama-server` around that time.
- **Step 4–5**: wizard and the live apply checklist on a 360 px wide screen.
- **Step 6**: Gluetun with ProtonVPN WireGuard and `PORT_FORWARD_ONLY=on`; the port-forward
  hook sets qBittorrent's listen port. Confirm with `docker logs orama-gluetun` (forwarded port
  line) and qBittorrent → Options → Connection. If "Send Prowlarr through the VPN" was ticked,
  Prowlarr's indexer proxy uses Gluetun's HTTP proxy.
- **Step 8**: Plex PIN sign-in (`/plex/sign-in`), server claim, the advertised LAN address
  (`customConnections`), and no duplicate libraries after a second apply (step 14 and 16 both
  re-apply).
- **Step 9**: Jellyfin first-run completed by Orama. Optional extra: recovery of an
  already-configured Jellyfin through `set-secret user:jellyfin` / `password:jellyfin`.
- **Step 10**: Intel Quick Sync. Plex shows `(hw)`; in Jellyfin check Dashboard → Playback →
  Transcoding for QSV/VAAPI and the ffmpeg log. If the owner had to switch it on, that is a
  finding: Orama passes the device and group but may not enable the option.
- **Step 11**: single-episode monitoring (Plan 4 review focus 2), real download clients, import
  with a hardlink. Check the inode by hand if anything looks copied:
  `stat -c '%i %h' <download file> <library file>`.
- **Step 12**: Bazarr, Greek (`el`) then English (`en`), SRT only. "Ready · EN subs, still
  looking for EL" is the honest fallback label.
- **Step 13**: the S95F device profile (no DTS, no TrueHD; Dolby Vision per the profile). Plex
  Dashboard must show Direct Play for video and audio.
- **Step 14**: installer idempotence on UGOS: same container ids, same compose files.
- **Step 15**: the Agent's `self-update-pull` then `self-update` (Plan 5 notes). A stopped
  container named `orama-updater` afterwards is normal: it is the record of the last update.
  This is the only place a real download of a newer published image is exercised.
- **Step 16**: live-region behaviour of the apply checklist and focus on the confirm page
  (WCAG 2.1 AA, Plan 3 Task 12 carry-forward).
