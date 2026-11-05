# Run SkyBird in the cloud (PC can be off)

GitHub runs SkyBird for free every morning:

1. At about 3:35 AM Central, right after the flight day ends (3:30 AM to 3:29 AM), it fetches the previous day's flights and makes the 4 videos.
2. It schedules them in your **Buffer** queue for 9 AM, 11 AM, 1 PM and 6 PM, with the day's captions, hashtags and @tags.
3. When it's done (about 3:45–4 AM) your phone gets a notification (ntfy). Open the **Buffer app** to look over, edit or delete any post before it goes out.

Setup takes about 30 minutes, once.

---

## 1. Make the GitHub account and repository

1. Go to **github.com/signup** and create the account. Verify the email.
2. Top right **+** → **New repository**.
   - Name: `skybird`
   - Choose **Private** (keeps your captions and settings to yourself; it's still free).
   - Leave everything else as is → **Create repository**.

## 2. Upload SkyBird

1. On the PC, unzip **skybird-cloud.zip**. Open the `skybird-cloud` folder so you see `.github`, `cloud`, `skybird`, `events.toml`, `requirements.txt`…
2. On the new repository's page, click **uploading an existing file**.
3. Select **everything inside** the `skybird-cloud` folder (Ctrl+A) and drag it onto the page. Wait for the list to finish, then click **Commit changes**.
4. Check that a folder called **.github** shows in the file list. If it doesn't:
   **Add file → Create new file**, type the name `.github/workflows/skybird-daily.yml`, paste in the contents of
   `skybird-daily.yml` from the `skybird-cloud` folder, and click **Commit changes**.

## 3. Add your keys (secrets)

In the repository: **Settings → Secrets and variables → Actions → New repository secret**. Add these four, one at a time (Name exactly as written, then paste the value):

| Name | Where to find the value |
|---|---|
| `OPENSKY_CLIENT_ID` | On the PC, open `Sky Bird\skybird-autopost\skybird-autopost\secrets.json` with Notepad: the value after `"opensky_client_id"` (without the quotes). Or opensky-network.org → your account → API client. |
| `OPENSKY_CLIENT_SECRET` | Same file: `"opensky_client_secret"` |
| `BUFFER_API_KEY` | Same file: `"buffer_api_key"`. Or Buffer → Settings → API. |
| `NTFY_TOPIC` | On the PC, `config.toml` → under `[notify]`, the `ntfy_topic` value (the same topic your phone is subscribed to). |

These stay encrypted in GitHub. They are never shown again, not even to you.

**Buffer channels: each city's own, by ID.** Each city posts only to the channel IDs in
`[buffer_channels]` in its profile (`cities/<city>.toml`). They're not secret. A city never falls back
to another city's channel, and SkyBird never picks "the TikTok channel" by kind, because the
account has two TikToks. A platform without an ID is not posted to.

| City | Channel | ID |
|---|---|---|
| Omaha | Facebook (SkyBird Omaha) | `6abc75edea19ca0bde2ccf21` |
| Omaha | TikTok (skybirdomaha) | `6abc7609ea19ca0bde2ccfc7` |
| Kansas City | TikTok (skybirdkansascity) | `6aebeae4e29c69a171682de3` |

To list the account's channels and IDs: Actions → **SkyBird post check** → Run workflow →
tick **Only look up the Buffer channels** → Run.

A `BUFFER_CHANNEL_<CITY>_<PLATFORM>` secret, for example `BUFFER_CHANNEL_KC_TIKTOK`, replaces the
profile's ID for that channel. SkyBird refuses:
- an ID given to two cities or two platforms,
- an ID that isn't on the Buffer account,
- an ID for the wrong kind of channel.

## 4. Test it (nothing gets posted)

1. Click the **Actions** tab. If GitHub asks, click **I understand my workflows, go ahead and enable them**.
2. Click **SkyBird daily posts** on the left → **Run workflow** → tick **Test run** → **Run workflow**.
3. It takes about 15 minutes. A green check = it worked, and your phone gets "SkyBird (cloud): 4 posts scheduled [dry run]".
4. Click the finished run → scroll to **Artifacts** → download **skybird-videos** to watch the 4 videos.

A red ✕? Click it, then the failed step; the last lines say what's wrong (usually a missing or mistyped secret).

## 5. Go live

1. **Important:** on the PC, open SkyBird → **Settings** → turn **Auto-posting off**. Otherwise the PC and the cloud would both post.
2. That's it. Tomorrow morning GitHub runs on its own. To post today, run it like step 4 **without** ticking Test run.

---

## Day to day

- **Review:** after the morning notification, open the Buffer app → **Queue**. Tap a post to edit the caption or delete it.
- **Change post times, hashtags or tags:** in GitHub open `cloud/config.toml` → pencil icon → edit → **Commit changes**. The next morning uses the new values.
- **Events:** edit `events.toml` the same way.
- **Run it again today** (for example after deleting the posts in Buffer): Actions → SkyBird daily posts → Run workflow → tick **Run again even if today's posts were already scheduled**.
- **Bad data days:** if OpenSky's numbers look incomplete, the notification warns you so you can delete the posts in Buffer. To skip those days automatically, set `skip_incomplete = true` in `cloud/config.toml`.
- **Cost:** free. Each morning uses about 15–20 of the 2,000 free minutes a month.

## Good to know

- **The repository must be public.** Buffer doesn't keep its own copy of a scheduled video; it fetches the link again at post time. SkyBird stores each day's videos on this repository (Releases → `videos-YYYY-MM-DD`, removed after 14 days) so the link never expires, and GitHub only lets Buffer fetch them from a public repository. Your keys stay private (secrets are never shown). Settings → General → Danger Zone → Change visibility → Public.
- **Hourly check ("SkyBird post check"):** runs every hour from about 6 AM to 8 PM.
  - **Just in time:** each post goes into Buffer about 2½ hours before its time, so only a few
    posts wait in Buffer at once (the free plan allows 10; `max_scheduled` in `[buffer]`).
    The morning notification lists the posts that are ready; you hear again only if one can't
    be scheduled. Review a post in Buffer's Queue once it's there. To put the whole day in at
    once again, set `just_in_time = false`.
  - **Did it go out:** from about 15 minutes after each post time, it asks Buffer whether the
    post went out, and your phone gets an alert if not.
- **Buffer budget:** at most 60 Buffer calls a day, all cities together (`[buffer]` in
  `cloud/config.toml`).
  - The last 20 are kept for scheduling posts.
  - When Buffer says less than 20% of a rate window is left, only scheduling goes ahead until
    that window resets.
  - The numbers (views and likes) are read once a day in a single batched call, plus one
    channel-totals call per channel. A normal day uses about 20 calls.
  - The weekly report shows the daily use.

- GitHub's clock can run 5–30 minutes late at busy times. Posts are scheduled in Buffer, so they still go out on time; if the job itself runs after a post's time, that post goes out about 10 minutes later instead, with the rest an hour apart (same catch-up rule as the PC).
- In the cloud, every post uses the day's default caption style (it rotates, so the caption tests keep running) and the pre-checked @tags. There's no approval step: Buffer's queue is your approval.
- Engagement charts, best-time testing and phone approvals live in the PC dashboard, which keeps working whenever the PC is on (with Auto-posting off it just shows things).

## Daily posts and new concepts

Omaha posts **3 a day** to Facebook and TikTok: 9 AM Eppley, 11 AM private planes and 6 PM
all airports, plus the **Sunday wrap** at 10 AM. Instagram is paused (`[instagram] enabled =
false` in `cloud/config.toml`; set it to true to bring it back). The 1 PM Offutt post is
**occasional**: it only goes out on dates listed under `[posts_on]`, for example
`"2026-11-11" = ["commercial", "private", "military", "all"]`.

Each day `per_day` of the 3 posts (`[testing]`, 1.5 = 1 and 2 on alternate days) are swapped
for new concepts. Which post stays regular rotates daily, and each post takes the concept that
has run least so far, scaled by `[testing.weights]` (2 = about twice as often, 0 = paused).

| Post | Concepts it rotates through |
|---|---|
| 9 AM Eppley | #01 Guess the destination, #02 Farthest flight, #03 First up, last down |
| 11 AM Private planes | #30 Which jets fly Omaha, #14 Weekend arrivals (Tuesdays) |
| 6 PM All airports | #10 Metro scoreboard, #06 Busiest hours |
| extra 10 AM Sunday | #07 Sunday weekly wrap (`[slots.wrap]`) |

Fixed dates under `[formats]` win over the rotation (the Lincoln game-weekend post on the
Sunday after a Husker home game). If a concept can't be made on its day, the regular post goes
out instead and the run shows a "New format" warning. The weekly report compares each concept
with the regular posts. (Oct 28 – Nov 4 tested 1 to 4 posts a day; that test is over.)

## Cities

SkyBird runs one engine for several cities. Each city has a profile in `cities/<city>.toml`
(Omaha is the default: every command without `--city` runs Omaha):

| City | Profile | Status |
|---|---|---|
| Omaha | `cities/omaha.toml` | live on Facebook + TikTok |
| Kansas City | `cities/kc.toml` | quiet build from Nov 6, 2026: `dry_run = true`, posts nothing |

What a profile holds:
- **Who it is:** name, brand ("SkyBird Kansas City"), handle, brand hashtag.
- **Where it posts:** `platforms`, the platforms the city posts to. A city never posts anywhere
  else, and never on another city's account.
- **Its airports:** `[airports]` as `ICAO = [short name, role]`, with role hub, ga, business or
  military. A city without a military field has no military post, and military flights are
  left out of its counts entirely.
- **What it never counts:** `exclude`, for example Kansas City's `KSZL`, Whiteman AFB: never
  counted, never shown.
- **Its map:** `map_center` and `towns` (the town labels on its map).
- **Its own settings:** `[config.<section>]` replaces that section of `cloud/config.toml` for
  the city, for example its post times (`[config.slots.*]`), hashtags and fixed lineup
  (`[config.daily_formats]`).
- **Accounts it may tag:** `[accounts]`. Airlines are shared by every city.
- **Its events:** `events/<city>.toml`.

Each city's files stay apart:
- **Data:** `cloud/data/<city>/`.
- **Videos:** `videos/<city>/<date>/` on GitHub Pages.
- **Reports:** `reports/<city>/latest.html`. Omaha's report is also still at `reports/latest.html`.
- **Shared:** the aircraft-type cache (`cloud/data/aircraft_cache.json`) is shared, since the
  same jets fly into both cities.

Any field with fewer than 5 takeoffs and landings is shown only as "Other fields".

**To add a city:**
1. Copy `cities/kc.toml` and change the values. Check every ICAO code in `airportsdata`.
2. Add `events/<city>.toml`.
3. Add the city to the `matrix: city:` list in the three workflows in `.github/workflows/`, and
   its `BUFFER_CHANNEL_<CITY>_<PLATFORM>` lines to the `env:` of the step that runs SkyBird in each.
   Put its Buffer channel IDs under `[buffer_channels]` in its profile once they're connected.
   The free plan has 3 channels.
4. Start it with `dry_run = true` and check a couple of weeks of sample videos before it posts.

Run one city by hand with `python -m skybird --config cloud/config.toml --city kc cloud-run --dry-run`.

### Kansas City: quiet build, launch, day-30 review

**Quiet build (14 days, dry run).** Kansas City runs every day with Omaha, right after it,
but posts nothing. Each morning:
- **Sample page:** its videos and a sample page go on GitHub Pages at
  `reports/kc/sample.html`.
  - The page lists every day so far: what would have posted and when, linked to the video
    (videos are kept for 7 days), KCI's counts, and OpenSky use.
  - The morning notification links to it.
- **Traffic check:** compares SkyBird's average day at KCI with KCI's own published traffic.
  - The reference is `[traffic]` in `cities/kc.toml`: about 9,750 operations a month, from
    117,030 in 2025. Confirm it against flykc.com's traffic statistics.
  - Expected range: 70–115%, since OpenSky misses some small planes. Outside it, the check
    says LOW or HIGH. Look into that before launch.
- **OpenSky use** for KC shows on the sample page and in the weekly report. It counts
  against the shared OpenSky budget.

**Launch** (when you say so):
1. In Buffer, connect Kansas City's TikTok. The free plan has 3 channels, so a paused one
   (Instagram) has to be disconnected first.
2. Add the `BUFFER_CHANNEL_KC_TIKTOK` secret. Get its value from the "Only look up the Buffer
   channels" run.
3. In `cities/kc.toml`, set `dry_run = false` and `launch_date = "YYYY-MM-DD"` (the first
   post day).

Kansas City then posts 3 times a day, 15 minutes after Omaha's posts (9:15, 11:15 and 6:15).

**Day-30 review.** From the launch date, the weekly report has a "Kansas City launch review"
section. It shows progress until day 30, then the verdict. The bars are in `[review]` in
`cities/kc.toml`:
- **Channels:** keep a channel if its average views per post are at least 25% of the best
  KC channel's, or at least 150.
- **Success** means both of these:
  - TikTok views grew week over week: the last full week beat the one before.
  - At least one post passed 3,000 views.
- **Facebook**, if turned on, must also average at least 1,400 views a day.

## OpenSky budget

The OpenSky account allows about **4,000 requests a day**, shared by every city. SkyBird
counts every request in `cloud/data/opensky_ledger.json` (per day, city and category, shared
by all runs) and stops itself before a limit:

| Limit per day | Default |
|---|---|
| All of SkyBird | 3,999 (never above the account's 4,000) |
| Each city | 1,600: flight lists 40, arrival fill-in 600, aircraft types 400, backfill 60, the rest headroom |
| Shared reserve | what's left (manual runs from the Actions tab may use it) |

The limits are under `[opensky_budget]` in `cloud/config.toml`, so changing them needs no code change.

How the limits are applied:
- **Flight lists come first.** Fill-in, type lookups and backfill stop before they could use
  the requests another city still needs for its day's lists.
- **OpenSky's own count wins.** SkyBird reads the `X-Rate-Limit-Remaining` header on every answer.
  - Under 400 left, fill-in and type lookups stop.
  - Under 100 left, only a city that doesn't have its day yet may pull its lists.
- **A 429 ("limit reached")** stops all OpenSky work until the reset, and the run uses the flight
  list it saved earlier. The 429's retry time lines the ledger's day up with OpenSky's reset.
- **Alerts:** a phone alert goes out when a day passes 80% of a limit: 3,200 for all cities, 1,280 for one city.
- **Where to see it:**
  - Each run's notes show the day's use so far.
  - The weekly report has an "OpenSky use" table.

Anything else using the same OpenSky account, such as a manual test or the old PC version,
counts against the same 4,000. SkyBird only sees that through OpenSky's header, so keep other use low.
