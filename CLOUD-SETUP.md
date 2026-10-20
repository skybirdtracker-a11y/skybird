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

- GitHub's clock can run 5–30 minutes late at busy times. Posts are scheduled in Buffer, so they still go out on time; if the job itself runs after a post's time, that post goes out about 10 minutes later instead, with the rest an hour apart (same catch-up rule as the PC).
- In the cloud, every post uses the day's default caption style (it rotates, so the caption tests keep running) and the pre-checked @tags. There's no approval step: Buffer's queue is your approval.
- Engagement charts, best-time testing and phone approvals live in the PC dashboard, which keeps working whenever the PC is on (with Auto-posting off it just shows things).
