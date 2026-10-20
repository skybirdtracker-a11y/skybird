# Prompt for the Claude Code session on your PC that makes the videos

Paste everything below the line into Claude Code on your PC. Use the same kind of session that made the
Sept 22 SkyBird videos, where it can see your "Flight Visibility" folder.

---

Set up the daily SkyBird video render so it feeds my auto-poster.

1. Find the script(s) that built the SkyBird daily videos for Sept 22 (the "Rebuild Sept 22 daily images and
   videos" job) in C:\Users\joshu\OneDrive\Desktop\Flight Visibility\Flight Visibility Data Company.
2. Make ONE command that renders a given flight day:  python render_skybird.py --date YYYY-MM-DD
   (default = yesterday, America/Chicago). It must save into
   C:\Users\joshu\OneDrive\Desktop\Sky Bird\videos\<YYYY-MM-DD>\  exactly these files:
   - post1_commercial.mp4  (Eppley commercial: zoom in on OMA -> departures fan out -> arrivals zoom back in -> stats -> end card)
   - post2_private.mp4     (private jets, "Owner" is labeled "Passenger")
   - post3_military.mp4    (military)
   - post4_all.mp4         (all airports, all planes: "Every bird flying in Omaha")
   Format: 1080x1920 vertical, H.264 + AAC (silent audio track is fine), 5+ seconds per frame, under 90 s.
   Name on screen: "SkyBird - Omaha". End card: the SkyBird name with the paper-airplane logo in the small circle.
   No data-source footer (OpenSky/FAA), no air-ambulance note. Endpoints labeled with 3-letter airport codes.
3. Also write stats.json in the same folder:
   {"data_date": "YYYY-MM-DD", "posts": {
     "commercial": {"departures": N, "arrivals": N,
                    "top_destinations": [{"code": "DEN", "city": "Denver", "count": N}, ...],
                    "farthest": {"code": "SEA", "city": "Seattle", "miles": N}, "busiest_hour": "7 AM",
                    "top_airlines": [{"name": "Southwest", "count": N}, ...]},
     "private":  {same keys, plus "top_aircraft": [{"type": "Citation Latitude", "count": N}, ...]},
     "military": {same keys as private},
     "all":      {"flights": N, "airports": [{"code": "OMA", "name": "Eppley", "count": N}, ...],
                  "top_destinations": [...], "busiest_hour": "7 AM"}}}
   Leave out any field you don't have.
4. Run it once for yesterday and show me the folder.
5. Then tell me the exact command line so I can paste it into the SkyBird dashboard / config.toml as render_command
   (SkyBird runs it every morning at 6:00 before building captions).
