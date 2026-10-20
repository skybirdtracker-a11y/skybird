# SkyBird - Omaha · Auto-poster

The videos, captions and dashboard follow the brand spec in `docs/SKYBIRD-BRAND-SPEC.md`. That covers:
- the light "board" style: Sky Navy, Bird Blue, Tower Orange and Cloud, with Archivo Black Italic and JetBrains Mono (bundled in `skybird/assets/fonts`),
- the two caption voices,
- the brand promises: military is counts only, air ambulances are dropped, and there's no source footer on the graphics.

Posts the four daily flight videos to **Instagram, Facebook and TikTok**. Each morning you check them and approve with one tap, and the scheduler posts each one at its time.

| Time (Central) | Post | File the render step writes |
|---|---|---|
| 9:00 AM | 01 · Eppley Commercial | `post1_commercial.mp4` |
| 11:00 AM | 02 · Private Jets | `post2_private.mp4` |
| 1:00 PM | 03 · Military | `post3_military.mp4` |
| 6:00 PM | 04 · All Airports, All Planes | `post4_all.mp4` |

> **PC off or away?** SkyBird can run in the cloud for free (GitHub Actions) and schedule each day's posts in Buffer. See [CLOUD-SETUP.md](CLOUD-SETUP.md).

## How a day works

1. **6:00 AM:** SkyBird reads that day's videos and `stats.json`. It writes **3 caption options** for each post (Hook, Data and Question) and picks the hashtags. On holidays and event days it also adds a line about the event (see `events.toml`). The approval dashboard then opens in your browser.
2. **You:** open the dashboard at http://127.0.0.1:8787, or on your phone (see below). Watch the videos, pick a caption option and edit it if you like. Turn hashtags, tagged accounts and platforms on or off, then press **Approve all**.
3. **9 / 11 / 1 / 6:** each approved post goes to every platform it is set for. Results and links appear on its card. If one platform fails, the card turns red and a **Retry failed** button appears. Retrying only re-posts to the platform that failed.

Rules:
- If you don't approve a post, it doesn't go out.
- If you approve late, the posts you missed catch up: the first goes out right away and each one after it waits at least an hour after the one before it (`catchup_gap_minutes`). Example: approve everything at 2:30 PM and they post at 2:30, 3:30, 4:30 and 6:00 PM. Nothing posts after 10 PM (`last_post_time`); a post that would land later is marked missed. Set `late_approval = "skip"` to drop missed slots instead.
- **Skip today** cancels one post.
- **Post now** posts immediately.

> **Dry run is ON by default** (`dry_run = true`). Everything works end to end, but nothing is uploaded; the activity log shows what *would* have been posted. Once your accounts are connected and `python -m skybird check` shows all three, set `dry_run = false`.

## The dashboard (http://127.0.0.1:8787)

| Tab | What it's for |
|---|---|
| **Approve** | Today's 4 videos. Pick a caption, change hashtags, tags and platforms, then tap **Approve all**. You can also **Post now**, **Skip** or **Retry failed**. The tab label shows how many posts are waiting. |
| **Upcoming** | The next 14 days. Set each post to **Ask me**, **Approve** (pre-approved, posts automatically once its video is rendered) or **Skip**. There are one-tap buttons for a whole day, and the day's events show as tags. |
| **Engagement** | Views, likes, comments and shares, filtered by 7, 30 or 90 days and by platform. A daily-views chart per platform, average views for each of the 4 post types, and your top posts with links. It refreshes every 3 hours by itself. |
| **History & logs** | Every post from the last 30 days with each platform's result, and a searchable activity log (filter to Posts or Problems). |
| **Settings** | The Auto-posting switch (pause everything), Live vs dry run, post times, **auto-approve** for each post, accounts to tag, connection status, and how to connect each account. |

## 1. Install (Windows, one time)

1. Install Python 3.11+ from python.org and tick **Add python.exe to PATH**.
2. Unzip this folder somewhere permanent, e.g. `C:\Users\joshu\OneDrive\Desktop\Flight Visibility\skybird-autopost`.
3. Double-click `scripts\SETUP.bat`. **To update later:** extract the new zip over the old folder and double-click `SETUP.bat` again. (The window stays open so you can read any message.) It:
   - installs the requirements,
   - creates `config.toml`,
   - registers a background task so SkyBird starts at every login (and restarts if it stops),
   - opens the dashboard.
4. Open `config.toml` and check `[media] folder`. It must point to where your render step saves the four videos each day.
5. Set **Settings → System → Power → Sleep: Never** (when plugged in). The PC has to be awake at post times.

### Making the daily videos (built in)

SkyBird makes the 4 videos itself every morning at 6:00 from yesterday's flights (counted 3:30 AM to 3:29 AM, `flight_day_start` in the config), using OpenSky Network's free flight data. TikTok captions get the best 5 hashtags (TikTok's limit; see `[hashtags.tiktok]`).
1. Create a free account at **opensky-network.org**, then go to **My OpenSky → Account → API Client** and create a client.
2. In the dashboard, go to **Settings → Flight data (OpenSky)**, paste the client id and secret, and tap **Connect**.
3. On the **Approve** tab, tap **Make videos + build posts** to make them right now. It takes about 4 minutes.

**Make sample videos (preview)** uses made-up flights, stamped "SAMPLE DATA", so you can see the look. SkyBird refuses to post those live.

How flights are sorted:
- **Military:** US military transponder codes, or military callsigns such as RCH or SNOOP.
- **Commercial:** airline callsigns such as SWA123 or UAL456.
- **Excluded:** air-ambulance callsigns.
- **Private:** everything else.

Tune the lists under `[classify]` in `config.toml`. Each reel follows the brand spec. It's 24.5 s long, 1080×1920, with no audio (add a trending sound in the app):
- **Departures:** a county-level zoom pulls back to the US, with a live clock and counter.
- **Arrivals:** planes fly in, then the camera flies back into Omaha.
- **Stats:** the skyline band, badge and tiles.
- **End card:** the navy SkyBird circle.

The Offutt reel is **counts only**: a zoom on the base, a stats page with 7-day bars, then the end card.

If a day's data looks incomplete (Eppley ends before about 9 PM, or has fewer than 60 airline departures), the dashboard warns you and auto-approve is held for that day.

### What the render step has to produce (if you use your own renderer instead)

The render step saves files into `…/out/social/<data_date>/`. `data_date` is the flight day the videos show, which is yesterday by default.

- `post1_commercial.mp4`, `post2_private.mp4`, `post3_military.mp4`, `post4_all.mp4`: vertical 9:16 video, 1080×1920, H.264 + AAC, between 5 and 90 seconds.
- `stats.json`: the numbers the captions use. See `sample/media/stats.json` for the format (departures, arrivals, top destinations, farthest trip, busiest hour, top airlines/aircraft, per-airport counts). Every field is optional.

If the render step can run as a command, put it in `render_command`. SkyBird then runs it every morning before writing captions.

## 2. Choose how posts get published

Choose the method in **Settings → Posting method** (or `[publisher] backend` in `config.toml`). You can switch at any time.

| Method | Developer accounts? | Cost | What you do |
|---|---|---|---|
| **Buffer** (default) | **No** | **Free** (3 channels = Instagram + Facebook + TikTok) | Approve in the morning. That's it. |
| **Upload-Post** | **No** | Free for Instagram + Facebook (10 uploads/month). TikTok and unlimited posts need the Basic plan (about $16–24/month) | Approve in the morning. That's it. |
| **Manual post kit** | No | Free | Approve in the morning, then at each time slot tap the phone notification → Save video → Copy caption → post in the app → "I posted it" (about 30 s per app) |
| **Direct** | Yes (Meta + TikTok) | Free | Approve in the morning. That's it. (Sections 2b and 3 below.) |

### 2a. Connect through Buffer (free, no developer accounts)

1. Make sure Instagram is a **Professional** account linked to the SkyBird Facebook **Page**, and TikTok is **Business** or **Creator**.
2. Sign up free at **buffer.com**, then **Connect channels**: Instagram, the SkyBird Facebook Page, and TikTok.
3. In Buffer: **Settings → API**, then create a personal API key.
4. Double-click `scripts\connect_accounts.bat` and paste the key. It finds the 3 channels.
5. In the dashboard: **Settings → Posting method → Buffer**, then **Check connections**. Turn on **Live mode** when all 3 show Connected.

Buffer's API needs a web link to the video, so at post time SkyBird uploads the video to a free file host (litterbox.catbox.moe, where the link is random and deleted after 72 h; if it refuses, catbox.moe, 0x0.st, uguu.se and then tmpfiles.org are tried, and each link is checked to make sure it really serves the video), then hands Buffer that link. Views, likes, comments and shares are read back from Buffer every 3 hours for the Engagement tab.

### 2a-alt. Connect through Upload-Post (no developer accounts)

1. Make sure Instagram is a **Professional** account and is linked to the SkyBird Facebook **Page**. TikTok should be a **Business** or **Creator** account.
2. Sign up at **app.upload-post.com**. Create a profile called `skybird`, and inside it **Connect** Instagram, Facebook and TikTok. These are the normal "Log in with…" screens.
3. Open **API Keys** there and create a key.
4. Double-click `scripts\connect_accounts.bat` and paste the key. It picks your profile and Facebook Page automatically.
5. In the dashboard, open **Settings → Check connections**. When all three show Connected, turn on **Live mode**.

Engagement numbers for posts sent this way come from Upload-Post's analytics.

### Phone notifications (free, recommended for every method)

1. Install the **ntfy** app and subscribe to a hard-to-guess topic, e.g. `skybird-omaha-7q4m2`.
2. In `config.toml`, set `[notify] ntfy_topic` to that topic and `dashboard_url` to the address you use to open the dashboard on your phone.

Your phone then buzzes:
- at 6 AM when the posts are ready to approve (tap it and you land on **Approve all**),
- at each time slot in manual mode,
- whenever a post fails.

## 2b. Connect Facebook + Instagram directly (Meta developer app)

What you need first:
- A **Facebook Page** for SkyBird - Omaha.
- The Instagram account switched to **Professional (Business or Creator)** and **linked to that Page**. In Instagram: Settings → Account type and tools, then connect it to the Facebook Page.

Create the app (about 10 minutes):
1. Go to https://developers.facebook.com → **My Apps → Create App**. Choose **Other → Business**. Name it `SkyBird Autopost`.
2. Add the products **Instagram Graph API** (it may show as "Instagram") and **Facebook Login for Business**.
3. Copy the **App ID** and **App Secret** from App settings → Basic.
4. Open **Tools → Graph API Explorer**. Pick your app, then **Get User Access Token**, and tick these permissions:
   `pages_show_list, pages_read_engagement, pages_manage_posts, instagram_basic, instagram_content_publish, instagram_manage_insights, read_insights, business_management` (the two insights permissions power the Engagement tab).
   Approve the popup for the SkyBird Page and Instagram account, then copy the token.
5. Double-click `scripts\connect_accounts.bat`. Paste the App ID, App Secret and token when asked. SkyBird then:
   - swaps the token for a Page token that never expires,
   - finds your Instagram account ID,
   - saves both to `secrets.json`.

You're posting only to accounts you own, so the app can stay in **Development mode**. It doesn't need Meta's App Review. Keep your own Facebook login as an admin of the app.

## 3. Connect TikTok directly (TikTok developer app)

1. Switch the TikTok account to a **Business** or **Creator** account.
2. Go to https://developers.tiktok.com → **Manage apps → Connect an app**. Fill in the name, icon and description.
3. Add the products **Login Kit** and **Content Posting API**. Turn on **Direct Post**.
4. Scopes: `user.info.basic`, `video.upload`, `video.publish`, `video.list` (video.list powers the Engagement tab).
5. Redirect URI: it must be an **https** address. Any page you control works (e.g. `https://skybirdomaha.com/tiktok`), and it's fine if the page shows "not found". You only copy the address it lands on.
6. Under **Sandbox**, add your TikTok account as a target user so you can test straight away.
7. Run `scripts\connect_accounts.bat` (or `python -m skybird tiktok-auth`). Open the link it prints, press Authorize, then paste back the full address you land on.

**TikTok's audit:** until TikTok approves the app, the API can only post *private* ("only me") videos. So the config starts on `mode = "inbox"`:
- Each video lands in your TikTok inbox at its time slot, and you tap **Post** in the app. That takes about 10 seconds, and you can add trending sounds there.
- After you submit the app for audit and it's approved, switch to `mode = "direct"` for fully hands-off public posting.

TikTok access tokens last 24 hours. SkyBird renews them automatically; the refresh token is good for a year.

## 4. Use SkyBird on your iPhone

1. On the PC, open **Settings → Use SkyBird on your iPhone**, choose a PIN (4–8 digits), and turn on **Phone access**. SkyBird restarts itself.
2. Scan the QR code with the iPhone Camera app (or type the address shown into Safari) and enter the PIN. The PC itself never asks for it.
3. In Safari tap **Share → Add to Home Screen** for an app-style SkyBird icon.
4. If the iPhone can't connect: it must be on the same Wi-Fi; double-click `scripts\Allow phone access.bat` once on the PC (tap **Yes**); and the PC's Wi-Fi network should be set to **Private** in Windows.
5. **Away from home:** install Tailscale (free) on the PC and the iPhone with the same account. An **Anywhere** address + QR code then appears in Settings, and phone notifications open it. Nothing is opened to the public internet.

Five wrong PINs lock that device out for 5 minutes. The PIN is stored only as a salted hash.

## 5. Captions, hashtags and tagging accounts

- **Caption styles:** Hook ("Where did Omaha fly Thursday? ✈️ 161 flights…"), Data (a stats block) and Question (built to get comments). You can edit the wording in `skybird/captions.py`.
- **Hashtags:** edit the lists in `[hashtags.*]` in `config.toml`.
  - `default` tags start ticked; `optional` tags start unticked.
  - Each day, tags for the top destination cities are added automatically (unticked).
  - Event tags are pre-ticked.
  - Instagram is capped at 30 hashtags, TikTok and Facebook at 10.
- **Tagging accounts (@mentions):** add handles under `[mentions]`, separately for each platform. Look each one up on the app first; handles are often different on each platform. Worth tagging:
  - Eppley Airfield
  - Offutt AFB / 55th Wing
  - the airlines serving OMA (Southwest, United, Delta, American, Allegiant, Frontier, Sun Country)
  - Visit Omaha, and local TV stations (WOWT, KETV, KMTV)
  - Millard Airport, and Council Bluffs Airport
  - plane-spotting accounts

### Caption styles + tagging tests

- Every post offers **Friendly, Deadpan, Question, Did you know, Guess first** (not on military) and, on event days, a **Local tie-in**. The default rotates day to day so each style gets tested (Fridays stay Deadpan; event days lead with the tie-in). A rotating call to action ("Follow for Omaha's sky…", "Tag someone…", "Save this…", "Which flight next?") ends each caption.
- **@mentions are pre-checked sparingly**, so we don't wear out our welcome: @OMAairport on the commercial post (the 6 PM post only on event days); the day's top 1–2 airlines whenever they're in the data; Visit Omaha on the 6 PM post Tue/Thu/Sat, at most 3 times a week; local news (WOWT, KMTV, KETV, World-Herald) **only** on big "who's in town" events marked `news_tag = true` in events.toml (Berkshire, CWS, the Creighton home opener, Gonzaga, UConn, NCAA games, volleyball World Cup), one outlet per day taking turns, at most twice a week each. Never more than 4 per post, never on military, never Offutt or the 55th Wing. Weekly limits count what actually went out in the last 7 days. Confirm or add handles in **Settings → Accounts to tag**.
- Each published post records its caption style, call to action, @mentions and event, and **Engagement → Caption & tagging tests** compares them by views, shares + saves, comments and follows. A phone reminder on Nov 7 marks the 3-week check-in.

## 5b. What's working + best time to post (Engagement tab)

- **Fair scoring.** A post is judged once it's 2 days old. Its views are compared with your typical views on that platform in the 4 weeks around it, so a growing account doesn't make newer posts look better.
- **What's working** compares the four posts, the three caption styles and the days of the week, and lists the clear winners in plain sentences.
- **Best time to post.** SkyBird can only learn a better time if posts sometimes go out at other times. With **Test nearby times** on (the default), each post moves 1 or 2 hours earlier or later about one day in three. The posts stay in order, an hour apart, between 7 AM and 9 PM. The Approve page marks these with a "time test" tag.
- Once an hour has at least 3 posts and beats the usual time by 15% or more, its card says **Try …** with a **Post at … from now on** button. Turn on **Switch to a better time automatically** to let SkyBird make the change itself (it waits for 4+ posts and a 20% lead) and send you a phone notification.
- **Every Monday** a phone notification sums up the week's winners.

## 6. Events calendar

`events.toml` lists holidays and Omaha events from Oct 2026 to Dec 2027, including:
- Berkshire weekend
- the College World Series
- NCAA March Madness in Omaha
- Husker home weekends (Ohio State!)
- Thanksgiving and Christmas travel
- NORAD Santa
- Veterans Day
- the Air Force's 80th birthday

On those days (and during each event's lead-up), SkyBird:
- adds the event's line to the captions,
- ticks the event hashtags,
- shows bonus-post ideas at the top of the dashboard.

Events marked `confirmed = false` have estimated dates; re-check them a few weeks before. To add an event, copy a block.

## Commands

```
python -m skybird serve               dashboard + scheduler (the background task runs this)
python -m skybird prepare [--force]   build today's package now
python -m skybird status              what's approved / posted today
python -m skybird approve             approve everything pending (no edits)
python -m skybird post private        post one slot right now
python -m skybird check               confirm Facebook / Instagram / TikTok connections
python -m skybird uploadpost-setup    connect through Upload-Post (no developer accounts)
```

Files: `config.toml` holds settings. `secrets.json` holds tokens, so never share it. `data/<date>/state.json` records each day's approvals and results, and `data/activity.log` is the full log.

## Troubleshooting

| Symptom | Fix |
|---|---|
| "Video missing" on a card | The render didn't save that file to the `[media] folder` path. Check the date folder and the file names. |
| Instagram error `(#10) … permission` | Re-run `meta-setup` and make sure `instagram_content_publish` was ticked. |
| Instagram "Processing ERROR" | The video format was rejected. Use H.264/AAC, 9:16, 3–90 seconds and under 300 MB. |
| TikTok `unaudited_client_can_only_post_to_private_accounts` | Use `mode = "inbox"` until TikTok approves the app's audit. |
| Nothing posted at 9 AM | Check whether the PC was asleep, then look at `data/activity.log`. Approving late still posts, as long as it's within 4 hours of the time slot. |
