"""
RSS feed definitions for the morning brief pipeline.
Each entry: (name, url, category, authority_tier)
Authority tiers: A (3×), B (2×), C (1×) — used for feed authority scoring.
"""

FEEDS = [
    # ── Tech ──────────────────────────────────────────────────────────────────
    # A-tier: major tech news outlets
    ("TechCrunch",          "https://techcrunch.com/feed/",                         "tech",  "A"),
    ("The Verge",           "https://www.theverge.com/rss/tech/index.xml",           "tech",  "A"),
    ("Ars Technica",        "https://feeds.arstechnica.com/arstechnica/index",       "tech",  "A"),
    # Engadget REMOVED 2026-09-15: zero-yield. Fetched fine (20 entries/run) but every
    # entry is a ~116-char teaser, so the <200-char paywall guard zeroed all of them —
    # 0 matched articles ever. feed_doctor probed 4 live same-domain alternates
    # (/feed, /feed/, /rss, /rss/): all 116 chars. Teaser-only by publisher policy.
    ("Wired",               "https://www.wired.com/feed/rss",                       "tech",  "A"),
    # CNET REMOVED 2026-09-15: zero-yield, same disposition as Engadget. 25 entries/run at
    # ~84 chars; feed_doctor probed 5 live same-domain alternates (/feed, /feed/, /rss,
    # /rss/, ?feed=rss2) — all 84 chars. (Already flagged as teaser-only on 2026-09-05
    # when ZDNet was removed for the same reason.)
    # ZDNet REMOVED 2026-09-05: original feed 404s (4 consecutive failed runs).
    # feed_doctor probed 6 live same-domain alternates (/rss/news, /feed, /rss, ?feed=rss2):
    # all return 25 entries but teaser-only (~118-123 chars) -> zeroed by the paywall guard.
    ("MIT Tech Review",     "https://www.technologyreview.com/feed/",                "tech",  "A"),
    # B-tier: specialist / mid-tier tech
    ("TechRadar",           "https://www.techradar.com/feeds.xml",                  "tech",  "B"),
    # Digital Trends REMOVED 2026-09-05: HTTP 405 Method Not Allowed, also when probed
    # solo (last ok 2026-09-02). Server-side block, not a transient blip.
    ("TechXplore",          "https://techxplore.com/rss-feed/",                      "tech",  "B"),
    ("Tom's Hardware",      "https://www.tomshardware.com/feeds/all",                "tech",  "B"),
    ("Slashdot",            "http://rss.slashdot.org/Slashdot/slashdotMain",        "tech",  "B"),
    ("Techmeme",            "https://www.techmeme.com/feed.xml",                    "tech",  "B"),
    # Hacker News REMOVED 2026-09-15: zero-yield BY DESIGN — it is a link aggregator whose
    # RSS carries no article body at all, so every entry is zeroed by the paywall guard.
    # Nothing to replace; a full-content feed for HN would have to be a third-party
    # scraper. Techmeme (kept) covers the same "what is the industry talking about" slot
    # and does serve content.
    # C-tier: community / aggregator
    # Reddit r/technology REMOVED 2026-07-04: HTTP 429-blocked (4 consecutive failed runs).
    # Reddit r/programming SWAPPED OUT 2026-09-05 for r/LocalLLaMA (see the AI section).
    # Reddit rate-limits per IP: only ONE subreddit feed per run is reliably served, so this
    # is a swap, not an addition. Do not re-add a second subreddit without re-testing.
    # A-tier: Apple-focused (added 2026-05-31 — interest 'Apple' had no dedicated feed)
    ("9to5Mac",             "https://9to5mac.com/feed/",                            "tech",  "A"),
    ("MacRumors",           "https://feeds.macrumors.com/MacRumors-All",            "tech",  "A"),

    # ── Developer / Cloud ─────────────────────────────────────────────────────
    # REMOVED 2026-05-05: These feeds generate too many highly-technical articles
    # that are not relevant to the target audience (product/strategy focus).
    # Retaining block_keywords below as a fallback filter for dev articles
    # that slip through from other feeds.
    #
    # A-tier
    # ("Microsoft DevBlogs",  "https://devblogs.microsoft.com/feed/",           "tech",  "A"),
    # ("GitHub Blog",         "https://github.blog/feed/",                      "tech",  "A"),
    # B-tier
    # ("AWS News",            "https://aws.amazon.com/blogs/aws/feed/",          "tech",  "B"),
    # ("Apple Dev News",      "https://developer.apple.com/news/rss/news.rss",    "tech",  "B"),
    # ("NVIDIA Dev Blog",     "https://developer.nvidia.com/blog/feed/",          "tech",  "B"),
    # ("Docker Blog",         "https://www.docker.com/blog/feed/",               "tech",  "B"),
    # ("Kubernetes Blog",     "https://kubernetes.io/feed.xml",                  "tech",  "B"),
    # C-tier
    # ("Cloudflare Blog",     "https://blog.cloudflare.com/rss/",                "tech",  "C"),
    # ("InfoQ",               "https://feed.infoq.com/",                        "tech",  "C"),
    # ("Stack Overflow Blog", "https://stackoverflow.blog/feed/",               "tech",  "C"),
    

    # ── AI / LLM ───────────────────────────────────────────────────────────────
    # A-tier
    # Hugging Face Blog REMOVED 2026-09-15: zero-yield, and the worst offender — 862
    # entries and ~254KB fetched every run with content/summary/description ALL EMPTY at
    # source (avg 0 chars), i.e. 26% of everything fetched for zero contribution. It was
    # A-tier, so it looked like a strong AI source while supplying nothing. feed_doctor
    # found only one FULL-content candidate for this slot — Simon Willison, already
    # subscribed below. OpenAI news (147 chars), Google AI (98), DeepMind (21) are teasers.
    ("Ars Technica AI",     "https://arstechnica.com/ai/feed/",                     "ai",    "A"),
    # Added 2026-09-05: AI-only cut of The Verge — same-day on major model launches.
    ("The Verge AI",        "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml", "ai", "A"),
    # VentureBeat AI REMOVED 2026-09-05: HTTP 429 rate-limited, also when probed solo
    # (last ok 2026-09-02). Was recovered once before via the /category/ai/feed/ URL.
    # B-tier
    ("OpenAI Blog",         "https://openai.com/blog/rss.xml",                     "ai",    "B"),
    ("Simon Willison",      "https://simonwillison.net/atom/everything/",          "ai",    "B"),
    ("SemiAnalysis",        "https://www.semianalysis.com/feed",                    "ai",    "B"),
    ("Import AI",           "https://importai.substack.com/feed",                   "ai",    "B"),
    ("The Decoder",         "https://the-decoder.com/feed/",                        "ai",    "B"),
    # Added 2026-09-05: first-party open-weights lab — full writeup the hour a model ships.
    ("Qwen Blog",           "https://qwenlm.github.io/blog/index.xml",              "ai",    "B"),
    # AI News (smol.ai) REMOVED 2026-09-05: feed now returns HTTP 402 Payment Required
    # (went paid; 2 consecutive failed runs). Not recoverable without a subscription.
    # C-tier
    ("The Rundown AI",      "https://rss.beehiiv.com/feeds/2R3C6Bt5wj.xml",        "ai",    "C"),
    ("MarkTechPost",        "https://www.marktechpost.com/feed/",                   "ai",    "C"),
    # Added 2026-09-05 (swapped in for tech/r/programming): fastest signal on open-weights
    # releases, often hours ahead of press. C-tier so it cannot reach must-read on its own.
    # SOLE Reddit feed by necessity — see the r/programming note in the Tech section.
    ("Reddit r/LocalLLaMA", "https://www.reddit.com/r/LocalLLaMA/.rss",              "ai",    "C"),

    # ── Singapore ──────────────────────────────────────────────────────────────
    # REMOVED 2026-09-14 (Mothership, Vulcan Post, The Independent SG).
    # Not a feed failure — all three fetched fine and served full content. They were
    # structurally unreachable: "Singapore" was the only interests topic they could
    # hit, and it sits in BROAD_KEYWORDS (A4), which only counts a broad keyword when
    # a *specific* topic also matches. Measured over a live run: 19 raw keyword hits
    # across the three feeds -> 0 after scoping -> 0 matched, every single day.
    # The "Singapore" topic was dropped from interests.json and from BROAD_KEYWORDS at
    # the same time. To restore SG coverage you must do BOTH: re-add the feeds AND give
    # them a non-broad topic to match on (or remove "Singapore" from BROAD_KEYWORDS and
    # accept the human-interest noise A4 was introduced to kill).
    # NOTE: mainstream SG outlets (Straits Times, CNA, Business Times) only serve
    # <200-char teasers in RSS, so the paywall guard zeroes them — not worth adding.

    # ── Cameras ────────────────────────────────────────────────────────────────
    # A-tier  (DPReview removed 2026-05-31 — 403 bot-blocked)
    ("PetaPixel",          "https://petapixel.com/feed/",                          "cameras", "A"),
    # B-tier
    ("Fuji Rumors",        "https://www.fujirumors.com/feed/",                     "cameras", "B"),
    ("Fstoppers",          "https://fstoppers.com/rss.xml",                        "cameras", "B"),

    # ── EV ────────────────────────────────────────────────────────────────────
    # A-tier  (InsideEVs removed 2026-05-31 — 404)
    ("Electrek",           "https://electrek.co/feed/",                            "ev",    "A"),
    # C-tier: broad cleantech, down-weighted to limit noise
    ("CleanTechnica",      "https://cleantechnica.com/feed/",                      "ev",    "C"),

    # ── Gaming ─────────────────────────────────────────────────────────────────
    # A-tier
    ("PC Gamer",           "https://www.pcgamer.com/rss/",                         "gaming", "A"),
    ("IGN",                "https://feeds.feedburner.com/ign/all",               "gaming", "A"),
    # B-tier
    ("Rock Paper Shotgun", "https://www.rockpapershotgun.com/feed",                "gaming", "B"),
    ("Eurogamer",          "https://www.eurogamer.net/feed",                       "gaming", "B"),

    # ── GPU / Hardware ─────────────────────────────────────────────────────────
    # B-tier  (AnandTech removed 2026-05-31 — site defunct, feed returns 0 entries)
    ("TechPowerUp",        "https://www.techpowerup.com/rss/news",                 "gpu",   "B"),

    # ── General ───────────────────────────────────────────────────────────────
    # BBC World REMOVED 2026-09-15: zero-yield (33 entries/run, 0 usable); no working
    # replacement among 11 probed same-domain alternates. This empties the "general"
    # category — there is currently NO world-news source. That is a coverage gap by
    # decision, not an oversight: it was already contributing nothing. To restore it, find
    # a world-news feed serving >=200 chars of body and add it back here as "general".
]
