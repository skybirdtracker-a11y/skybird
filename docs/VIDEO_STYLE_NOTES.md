# Video style

The full brand spec is in `SKYBIRD-BRAND-SPEC.md` (reference video: `skybird-example-commercial-9x16.mp4`).
SkyBird's built-in renderer (`skybird/render.py`) follows it:

- Light board style: Cloud/Ice map, white title card + HUD cards, Archivo Black Italic headlines, JetBrains Mono data.
- Departures 7.5 s (county zoom -> US, live clock + counter) · Arrivals 7.5 s (US -> back into Omaha) ·
  Stats 6 s (skyline band + badge, 4 tiles, top-5 cities, 4 facts) · End card 3.5 s (navy SkyBird circle, follow pill).
- Military (Offutt) is counts only: Offutt zoom -> stats with 7-day bars -> end card. No routes, callsigns, tails or times.
- No source footer on the graphics (the data credit goes in the caption), no audio (add a trending sound in-app).
- Air ambulances and helicopters are dropped before anything is counted. Days with incomplete data are flagged.
