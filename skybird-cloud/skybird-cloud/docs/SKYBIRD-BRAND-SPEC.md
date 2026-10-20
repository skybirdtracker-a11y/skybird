# SkyBird - Omaha: brand spec for daily posts and reels

Hand this file and `skybird-example-commercial-9x16.mp4` to any chat or person making SkyBird content. The MP4 is the reference: when this doc and the video disagree, match the video.

- **Brand kit (source of truth for logo art):** https://claude.ai/artifact/T1TBVXWX2pPf5xKzMMjbdb
- **Working templates:** `Flight Visibility Data Company\tools\social\` (`reel-template.html`, `template.html`, `brand\`, `fonts\`)

---

## 1. Identity

| Item | Use |
|---|---|
| Name | **SkyBird - Omaha**. "SkyBird" is one word, capital S and B. In type: "SkyBird" in Sky Navy, "- Omaha" in Bird Blue. |
| Handle | `@skybirdomaha` (placeholder until the real handles are confirmed) |
| End-card line | "Every bird flying in Omaha." (the brand kit says "Every bird over Omaha."; the owner hasn't picked one yet, so ask) |
| Sub-line | "SkyBird - Omaha · new board every morning." |

## 2. Logo usage

| Asset | Where it goes |
|---|---|
| **End-card logo circle** | The small circle on the final card. Sky Navy `#014291` fill, 10 px Cloud border, white paper airplane on top, "SkyBird" below it in white Archivo 900 italic, "OMAHA" in Riverfront Mint JetBrains Mono, letter-spaced. **Always this on the end card**, never the skyline badge. |
| Skyline badge (`brand/badge.png`) | The circle overlapping the skyline band on stats slides and still posts. |
| Skyline backdrop (`brand/backdrop.png`) | The top band on stats slides and the top 46% of the end card (slow push-in). It already contains the dashed loop and a plane, so don't draw another loop on top of it. |
| Paper airplane icon | Inline SVG, 120×80 viewBox: `M4 38L116 4L62 76L50 48Z` plus the fold `M50 48L116 4`, white fill with a Bird Blue or Sky Navy stroke. Used beside the name, the kicker and the handle. |
| Primary wordmark PNG | **Don't use it.** The file has a checkerboard pattern baked into its background. Set the name in type instead. |

## 3. Color

| Name | Hex | Use |
|---|---|---|
| Sky Navy | `#014291` | Headlines, numbers, pins, planes, the end-card circle |
| Bird Blue | `#0999EA` | Headline accent word, kickers, departure routes, airline routes |
| Omaha Sky | `#1E8BE0` | Backdrop sky |
| Tower Orange | `#F28F23` | Callouts only: arrival routes, private-jet routes, the hub pulse ring, one highlighted stat. Never body text. |
| Riverfront Mint | `#AEE1D8` | Small accents on navy |
| Cloud | `#F6F2E7` | Page background for stats and end card, scene-fade color |
| Ink | `#0A2540` | Body text |
| Muted | `#46607A` | Labels, secondary text |
| Line | `#D9DEE5` | Dividers |
| Ice | `#E6F3FC` | Map land, bar tracks, fact tiles |
| Map sea / background | `#F3F7FB` | Map canvas; Canada and Mexico land `#EEF2F6` |

Text is Ink or Sky Navy. White text only on Sky Navy.

## 4. Type

| Role | Font |
|---|---|
| Headlines | **Archivo Black Italic** (900 italic), tight leading (0.95). The last word or phrase goes in Bird Blue (arrivals: Tower Orange). |
| Body | Archivo 600 |
| Data, kickers, labels, airport codes | **JetBrains Mono** 600/700, kickers UPPERCASE with 2–3 px letter-spacing |

The fonts are bundled in `tools\social\fonts\` and embedded into the page when it's built. Local render pages can't load web fonts over `file://`, so never rely on the Google Fonts link for rendering.

## 5. Video format

- **1080 × 1920, 30 fps, H.264 MP4**, about 24.5 s. No audio: add a trending sound in-app.
- **Safe zones (TikTok / Reels):** keep text out of the top 150 px and the bottom ~380 px. Keep key content off the right-hand icon column (x > 940, y 1000–1650). Put the HUD cards on the left.
- **Motion:** eased (cubic in-out). Cards and labels pop in with a slight overshoot. Numbers count up. Every scene after the first fades in from Cloud over 0.3 s. Nothing flashes or strobes.

## 6. Reel structure (all four posts)

| Scene | Length | What happens |
|---|---|---|
| **Departures** | 7.5 s | Opens zoomed in on the metro at county level (town names: Omaha, Council Bluffs, Bellevue, Papillion, Lincoln, Fremont). The camera pulls back to the whole US (and Canada/Mexico if a route goes there). Planes take off in real departure order. A clock (LOCAL TIME) and a counter (DEPARTURES n / total) tick up. Each endpoint pops in with its 3-letter code and a live ×N count. |
| **Arrivals** | 7.5 s | Starts on the whole map. Planes leave their home cities, timed to land at their real arrival times. The camera then flies back into Omaha. |
| **Stats** | 6 s | Cloud page with the skyline band and badge, a headline ("Eppley, by the numbers."), 4 stat tiles, top-5 city bars and 4 fact tiles. |
| **End card** | 3.5 s | Skyline band, the SkyBird logo circle, the end-card line, the sub-line and a "FOLLOW @skybirdomaha" pill. |

**Military reel is different:** Offutt zoom (5.5 s, counts only) → stats with weekly bars (6 s) → end card (3.5 s).

### Map components

- **Title card:** white, radius 30, soft shadow, at top 150 px. Brand row: plane + "SkyBird - Omaha", with the date (`TUE · SEP 22`) on the right. Then the kicker (mono, colored), then the headline.
- **Hub pin:** Sky Navy dot, white 4 px stroke, a pulsing Tower Orange ring, and a navy pill label with the code (`OMA`). Posts that cover several fields show the pins `OMA / MLE / CBF / OFF` when zoomed in and one `OMAHA` pill when zoomed out.
- **Endpoint labels:** white pill with a 2.5 px border in the route color. The 3-letter code is JetBrains Mono 700 in Ink, followed by "×N" in the route color. Drop the leading K on US codes (KORD → ORD); Canadian C becomes Y (CYYZ → YYZ). Labels dodge each other and the hub.
- **Planes:** top-down airliner silhouette, Sky Navy with a white outline, rotated to its heading. Trails are 3 px round-capped lines and dim to 45% once a flight lands. A busy route fans out as several slightly different arcs.
- **Route colors:** Commercial: departures Bird Blue, arrivals Tower Orange. Private: departures Tower Orange, arrivals Sky Navy. All airports: by type (airline Bird Blue, private jet Tower Orange, props + other Sky Navy) with a legend.

## 7. The four daily posts (Central time)

| Time | Post | Covers | Headlines (departures / arrivals / stats) |
|---|---|---|---|
| 9:00 AM | Eppley · Commercial | Airline + cargo flights at KOMA | "Where did Omaha fly yesterday?" / "And who flew into OMA." / "Eppley, by the numbers." |
| 11:00 AM | Private jets | Business jets at all metro fields | "Where Omaha's private jets went." / "Private jets flying in." / "Private jets, by the numbers." |
| 1:00 PM | Offutt · Military | Military movements at KOFF, **counts only** | "What flew at Offutt yesterday?" / (no arrivals scene) / "Offutt, by the numbers." |
| 6:00 PM | All airports + planes | Everything at KOMA, KMLE, KCBF, KOFF (military counted, never drawn) | "Every flight out of Omaha." / "And every flight coming home." / "The whole metro, by the numbers." |

Posts always cover **yesterday**. They go up only after the owner clicks approve in the morning.

## 8. Brand promises (non-negotiable)

1. **Birds, not people.** No owner names and no tail numbers, ever. Don't guess who's behind an LLC. Airlines and aircraft types are fine.
2. **Next day, never live.**
3. **Military: counts only.** No callsigns, tails, routes or times at Offutt. Always say the count is a floor.
4. **Medical stays private.** Air-ambulance flights are dropped before anything is counted.
5. **Facts first, joke second.** Never joke about people, the military or medical flights.
6. **No source footer on the graphics.** Credit the data in the caption instead (see below).

## 9. Voice and captions

Two voices (from the brand kit):
- **Friendly Neighbor** (default, Mon–Thu): warm, plain, local. *"Morning, Omaha. Eppley moved 147 airline flights on Tuesday…"* Sign-off: "See you up there."
- **Deadpan Tower** (Fridays, TikTok hooks, slow days): dry, straight-faced, one joke at most. *"Offutt: 5. The rest were apparently invisible."* Sign-off: "Roger, roger."
- A third option for reach: a **question hook** that ends by asking the audience something ("Which route do you fly most out of Eppley?").

**Caption layout:** caption text → (optional @tags) → hashtags → credit line:
> (Credit line removed from captions at the owner's request, Oct 2026.)

**Hashtags** (up to 25, Instagram max 30; captions max 2,200 characters):
- Every post: `#SkyBirdOmaha #Omaha #Nebraska #avgeek #aviation #planespotting`
- Commercial: `#EppleyAirfield #OMA #OmahaAirport` + the top airlines (`#SouthwestAirlines #UnitedAirlines`…) + the top cities (`#Chicago #Denver`…) + `#travel`
- Private: `#privatejet #bizjet #businessaviation` + aircraft families (`#CessnaCitation #Learjet`…) + `#EppleyAirfield #CouncilBluffs`
- Military: `#OffuttAFB #Offutt #55thWing #USAF #AirForce #Bellevue #militaryaviation`
- All: `#EppleyAirfield #CouncilBluffs #Millard #OffuttAFB #generalaviation #OmahaMetro #Iowa`

Don't invent @handles for airports or airlines. Only tag accounts the owner has verified.

## 10. Don'ts

- Don't use a dark theme. The first drafts used one; it's superseded.
- Don't use Inter, Big Shoulders or any other face; don't use gradient washes or emoji in the graphics.
- Don't use the checkerboard wordmark PNG, and don't draw a second dashed loop over the backdrop.
- Don't put the skyline badge in the end-card circle (use the navy SkyBird + paper-airplane circle).
- Don't post a day whose data looks incomplete. A full day at Eppley runs past about 10 PM with roughly 95–140 departures. Sept 23 stopped at 5:49 PM and had to be skipped.

## 11. Reference video: `skybird-example-commercial-9x16.mp4`

Eppley commercial reel built from Tuesday Sept 22, 2026 data (24.5 s):

| Time | Shows |
|---|---|
| 0:00–0:07.5 | Departures: county-level zoom on OMA pulls back to the US, 81 departures, clock 5:24 AM → 10 PM |
| 0:07.5–0:15 | Arrivals: 66 arrivals from across the US and Toronto (YYZ), camera flies back into OMA |
| 0:15–0:21 | Stats: 147 flights, 81 departures, 66 arrivals, 28 cities; top cities ORD / DEN / PHX / MSP / LGA |
| 0:21–0:24.5 | End card with the SkyBird - Omaha logo circle and the follow pill |
