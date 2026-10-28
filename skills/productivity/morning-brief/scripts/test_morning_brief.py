#!/usr/bin/env python3
"""Regression suite for morning-brief. Runs offline on a temp HOME — never touches
~/.hermes/morning-brief, the network, the LLM or Discord.

    python3 test_morning_brief.py          # prints PASS/FAIL per check, exit 1 on any FAIL

Covers the 1.10.0 changes (same-story test, twin-free selection, C-tier rule,
deterministic ranking, hold-back, ledger fields, Discord nonce, empty-day header
failure, meta-sentence filter, feed_doctor alignment, Accept-Encoding, wrapper lock,
fetch --dry-run) plus the invariants they touch.
"""
import atexit, json, os, shutil, sys, tempfile, time, types, importlib, random
sys.dont_write_bytecode = True          # no __pycache__ next to the (live) wrapper

REAL_HOME = os.path.expanduser("~")
TMP = tempfile.mkdtemp(prefix="mb-test-")
atexit.register(shutil.rmtree, TMP, True)   # every run left a dir behind in the Pi's /tmp
os.environ["HOME"] = TMP                       # BASE_DIR is resolved at import time
DATA = os.path.join(TMP, ".hermes", "morning-brief")
os.makedirs(DATA)
json.dump({"topics": ["OpenAI", "Claude", "Anthropic", "LLM", "Apple", "GPU"],
           "block_keywords": []}, open(os.path.join(DATA, "interests.json"), "w"))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import article_id as A          # noqa: E402
import fetch_rss as F           # noqa: E402
import send_brief as S          # noqa: E402
import summarize as Z           # noqa: E402
import feed_doctor as D         # noqa: E402

RESULTS = []
def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -- {detail}"))

NOW = time.time()
def art(title, content, source="Src", url=None, score=6.0, kw=2, tier="B", n=1, pub=None):
    return {"title": title, "content": content, "source": source, "tier": tier,
            "url": url if url is not None else f"https://{source.replace(' ', '').lower()}.com/{abs(hash(title))}",
            "published": pub or time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(NOW - 3600)),
            "_score": score, "_interest_kw_count": kw, "_num_sources": n}

# ── same_story ────────────────────────────────────────────────────────────────
FABLE = [
    art("Statement on the US government directive to suspend access to Fable 5 and Mythos 5", "Statement on the US government directive to suspend access to Fable 5 and Mythos 5 Well this is nuts : The US government, citing national security authorities, has issued an export control directive to suspend all access to Fable 5 and Mythos 5 by any foreign national, whether inside or outside the United States, including foreign national Anthropic employees", "Simon Willison"),
    art("US government forces Anthropic to disable Claude Fable 5 and Mythos 5 for all customers", "The US government has ordered Anthropic to shut down global access to Fable 5 and Mythos 5, citing alleged jailbreak risks. Anthropic is complying but pushing back publicly: the vulnerabilities are minor and exist in competing models like GPT-5.5, the company says", "The Decoder"),
    art("Anthropic shuts down Fable, Mythos models following Trump admin directive", "Anthropic completely shut off access to its Mythos 5 and Fable 5 models Friday night, just days after they were launched. The move comes after Anthropic's receipt of a US Commerce Department directive Friday evening", "Ars Technica"),
]
check("same_story: Fable-5 A~B", A.same_story(FABLE[0], FABLE[1]))
check("same_story: Fable-5 A~C", A.same_story(FABLE[0], FABLE[2]))
check("same_story: Fable-5 B~C", A.same_story(FABLE[1], FABLE[2]))

AUS1 = art("Australian PM says OpenAI hacked government health website", 'A rogue OpenAI model bypassed safeguards during training and hacked an Australian government website, Prime Minister Anthony Albanese said as he admonished the ChatGPT creator over an "obviously unacceptable" breach.', "TechXplore")
AUS2 = art("OpenAI agent “didn’t accept no for an answer” in Australian government breach", 'Australian Prime Minister Anthony Albanese said his government is investigating a June incident in which an OpenAI agent accessed "non-public files" from the country\'s online Medicare statistics portal. OpenAI said in a statement that "our models took actions we did not intend" in causing the breach', "Ars Technica")
check("same_story: Australia breach across outlets", A.same_story(AUS1, AUS2))

OPUS = art("Anthropic Launches Claude Opus 5.5 With Fable-Level Performance at Lower Cost", "Anthropic today released Claude Opus 5.5, a new frontier model that the company says matches Fable 5.1 on coding and reasoning benchmarks at a lower price, with a larger context window and faster output for API customers.", "MacRumors")
GPT6 = art("OpenAI's New GPT-6 Sol and Luna Models Bring Astra Improvements", "OpenAI on Tuesday rolled out GPT-6 Sol and GPT-6 Luna to ChatGPT Plus subscribers, adding Astra voice mode and a new memory feature; the company says Luna is cheaper to run and will reach free users next month.", "9to5Mac")
check("same_story: different launches with shared launch vocabulary are NOT merged",
      not A.same_story(OPUS, GPT6), A.same_story(OPUS, GPT6))

BOILER = "The information originates from Apple's own code (spotted by MacRumors contributor Aaron Perris) and shows clear product images of the new models."
MR = [art("iPad Mini 8 Leaked: A20 Pro Chip, Landscape Camera, and More", "Apple's next-generation iPad mini will feature the A20 Pro chip. " + BOILER, "MacRumors"),
      art("Exclusive: First Look at HomePod Mini 2 in Three New Colors", "Apple's new HomePod mini will come in five colors. " + BOILER, "MacRumors"),
      art("Apple Code Confirms iPad 12 Specs: A19, 8GB RAM", "The next low-cost iPad will have an A19 chip. " + BOILER, "MacRumors")]
A.annotate_story_tokens(MR)
check("same_story: same-outlet boilerplate does not merge different products",
      not A.same_story(MR[0], MR[1]) and not A.same_story(MR[0], MR[2]))
check("annotate_story_tokens: boilerplate words stripped", "perris" not in MR[0]["_story"], MR[0]["_story"])

E1 = art("OpenAI says images were removed", "OpenAI&nbsp;says &ldquo;most&rdquo; images were removed &mdash; agents", "Techmeme")
E2 = art("FTC chairman resists anthropomorphizing AI", "FTC&nbsp;Chairman &ldquo;resists&rdquo; &mdash; agents", "Slashdot")
check("same_story: HTML entities are not shared words", not A.same_story(E1, E2))
check("clean_text: double-encoded entity", A.clean_text("Google&amp;#8217;s") == "Google’s")
check("title_overlap: entity-decoded", A.title_overlap("Google&#8217;s AI", "Google’s AI") == 1.0)

# ── extraction ────────────────────────────────────────────────────────────────
import feedparser  # noqa: E402
rss = b"""<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>t</title>
<item><title>Google&amp;#8217;s new thing</title><link>https://x.com/a</link>
<description>&lt;p&gt;Body&amp;nbsp;text &amp;amp; more&lt;/p&gt;</description></item></channel></rss>"""
arts_, _u = F._extract_articles(feedparser.parse(rss), "X", "B")
check("_extract_articles: title entities decoded", arts_[0]["title"] == "Google’s new thing", arts_[0]["title"])
check("_extract_articles: content tags + entities stripped", "<" not in arts_[0]["content"] and "nbsp" not in arts_[0]["content"], arts_[0]["content"])

# ── classify / deduplicate ────────────────────────────────────────────────────
WORDS = ("falcon quartz meadow lantern cobalt orchid glacier saffron tundra velvet "
         "harbor pepper cinder marble willow comet ember fjord maple onyx prism rune "
         "sierra thistle umber vortex wren yarrow zephyr basalt canyon delta").split()
def uniq(i, n):
    rnd = random.Random(i)
    return " ".join(rnd.sample(WORDS, n)) + f" item{i}"
def pool():
    p = []
    for i in range(12):
        p.append(art(uniq(i, 4).title(), (uniq(100 + i, 12) + " ") * 3,
                     source=f"Outlet{i % 6}", score=20 - i, kw=3 if i < 4 else 1))
    # a twin of story 0 from another outlet, ranked 2nd
    p.insert(1, dict(p[0], title=p[0]["title"] + " Again", source="OutletX",
                     url="https://outletx.com/twin", _score=19.5))
    return p
must, may = F.classify([dict(a) for a in pool()])
titles = [a["title"] for a in must + may]
check("classify: twin skipped, slots still full (3 + 5)", len(must) == 3 and len(may) == 5
      and not any("again" in t for t in titles), (len(must), len(may), titles))
check("classify: no two picks are the same story",
      all(not A.same_story(a, b) for i, a in enumerate(must + may) for b in (must + may)[i + 1:]))

c_alone = art("Reddit post about Claude and OpenAI and LLM", "x" * 300, "Reddit r/LocalLLaMA", tier="C", score=50, kw=5)
c_corr  = dict(c_alone, _num_sources=2, title="Another reddit post Claude OpenAI LLM", url="https://r.com/2")
m2, _ = F.classify([c_alone] + [dict(a) for a in pool()])
check("classify: uncorroborated C-tier not must-read", c_alone not in m2)
m3, _ = F.classify([c_corr] + [dict(a) for a in pool()])
check("classify: corroborated C-tier may be must-read", any(a is c_corr for a in m3))

ties = [art(f"Tie item {i} unique{i}", f"lede{i} " * 40, f"S{i}", score=9.0, kw=3, tier="B",
            pub=f"2026-09-2{i}T00:00:00+00:00") for i in range(6)]
orders = set()
for _ in range(5):
    random.shuffle(ties)
    orders.add(tuple(a["title"] for a in F.classify([dict(t) for t in ties])[0]))
check("classify: ties ranked deterministically (independent of input order)", len(orders) == 1, orders)

chain = [art("Leaks reveal a new Apple HomePod mini, iPad mini, and Apple TV 4K", "Apple leaks homepod ipad appletv " * 10, "The Verge", score=9),
         art("iPad Mini With A20 Pro, New HomePod Mini Colors, and New Apple TV 4K All Just Leaked", "leak ipad homepod appletv " * 10, "MacRumors", score=9),
         art("Deals: iPad mini $100 off at Amazon", "deal ipad mini homepod appletv " * 10, "9to5Mac", score=3)]
d = F.deduplicate([dict(a) for a in chain])
check("deduplicate: promo post never counts as corroboration",
      all(x["_num_sources"] <= 2 for x in d), [(x["title"], x["_num_sources"]) for x in d])

ORB = art("Google's orbital AI data center test packs four TPUs and 1,000W of solar power",
          "Google is set to launch an experimental satellite, named MVP, into orbit next week to test running an AI data center in space. As part of a wider initiative called Project Suncatcher , the satellite will be aboard a SpaceX Falcon 9 rocket when it launches from the Vandenberg Space Force Base in Cali", "Tom's Hardware")
CHN = art("China launches 'Supercomputing-1' AI satellite as orbital data centers gather momentum",
          "China has launched nine satellites — including its first integrated “rocket and satellite” AI computing project — as part of its push to put AI computing in orbit. According to a Digitimes report , the Kinetica 1 Y18 rocket, operated by Chinese commercial launch provider CAS Space, lifted off on Sep", "Tom's Hardware")
check("same_story: same-outlet near-vocabulary distinct stories NOT merged", not A.same_story(ORB, CHN), A.same_story(ORB, CHN))
check("promo: news with 'deal' in the title is not a promo",
      not F._PROMO_RE.search("Anthropic signs $11.6 billion cloud deal with Akamai"))
check("promo: deal round-ups are promos", F._PROMO_RE.search("Deals: M5 MacBook Air $150 off")
      and F._PROMO_RE.search("MacRumors Giveaway: Win an Apple Watch"))
check("title_tokens: plural fold (avatars ~ avatar)", "avatar" in A.title_tokens("Gemini Live Avatars"))
check("title_tokens: aggregator attribution stripped", "york" not in A.title_tokens("Nvidia talks (New York Times)", "Techmeme"))
tie = [art("OpenAI agent breach at Australian government health site", "OpenAI agent breach Australian government health portal Albanese said " * 4, s_, score=8.0, kw=2)
       for s_ in ("TechXplore", "Tom's Hardware", "Ars Technica")]
reps = set()
for _ in range(6):
    random.shuffle(tie)
    reps.add(F.deduplicate([dict(t) for t in tie])[0]["source"])
check("deduplicate: cluster representative independent of arrival order", len(reps) == 1, reps)

raw = [{k: v for k, v in a.items() if not k.startswith("_")} for a in tie + list(FABLE)]
for a in raw:
    a["content"] = (a["content"] + " ") * 2
try:
    A.annotate_story_tokens(raw)
    mm, _o, _h = F.match_articles(raw, {"topics": ["OpenAI", "Anthropic", "Australian"]}, set(), [])
    dd = F.deduplicate(mm)
    m_, y_ = F.classify(dd)
    ok = len(mm) >= 4 and all("_num_sources" in a for a in dd)
except Exception as e:  # noqa: BLE001
    ok, e_ = False, e
check("end-to-end: match -> deduplicate -> classify on raw extracted articles", ok,
      locals().get("e_"))

check("promo: 'deal on' / FDA clearance / discount news are NOT promos",
      not any(F._PROMO_RE.search(t) for t in ["EU reaches deal on AI Act rules",
              "Apple Watch hypertension feature gets FDA clearance", "Nvidia offers discount to China buyers"]))
check("_caps2: common two-letter caps excluded, UN/EU kept",
      A._caps2("(PR) Tesla EV sales slump in EU") == {"eu"} and "un" in A._caps2("CEOs urge UN on AI"))
check("_caps2: ALL-CAPS headline ignored", A._caps2("BREAKING: IT IS HAPPENING TO US") == set())
check("title_tokens: Slashdot parenthetical kept", "ram" in A.title_tokens("Macs Get Faster (And Boards Now Locked to RAM)", "Slashdot"))
big = []
for i in range(1200):
    rnd_ = random.Random(i)
    big.append(art(" ".join(rnd_.sample(WORDS, 5)) + f" x{i}", " ".join(rnd_.sample(WORDS, 20)) * 2,
                   f"Src{i % 30}", score=float(i % 9)))
A.annotate_story_tokens(big)
t0 = time.perf_counter()
F.deduplicate(big)
el = time.perf_counter() - t0
check(f"deduplicate: 1,200 candidates in {el:.2f}s (< 20 s even on the Pi)", el < 20, el)

W1 = art("How to watch AFL Grand Final 2026 for FREE: Live streams", "Watch AFL Grand Final 2026 FREE on 7Plus (Australia) Unlock your stream with Surfshark Brisbane vs Fremantle kick-off: Sat, Sep 26 at 12.30am ET / 5.30am BST / 2.30pm AEST Two opposing senses of destiny collide in the 2026 AFL Final at Melbourne Cricket Ground. Fremantle's first ever flag would be h", "TechRadar")
W2 = art("How to watch Hungary vs Ukraine: FREE streams, TV channels for Nations League clash", "Watch Hungary vs Ukraine free on M4 Sport (Hungary) and L'Equipe Live Foot (France) Unlock your stream with NordVPN (save up to 75%) Kick-off: Fri, Sep 25, 2.45pm ET / 7.45pm BST Hungary and Ukraine had to watch the first ever 48-team World Cup from back home, so both sides will be chomping at the b", "TechRadar")
check("same_story: same-outlet template pages (How to watch X vs Y) NOT merged", not A.same_story(W1, W2), A.same_story(W1, W2))
check("comparison: U.N. / United Nations -> UN", A._norm("The U.N. and the United Nations") == "The UN and the UN")
check("clean_text: displayed wording untouched", A.clean_text("The U.N. said") == "The U.N. said")
check("promo: Walmart/price-drop/Prime Day shapes are promos",
      all(F._PROMO_RE.search(t) for t in ["RTX 5070 Ti PC Drops to $1,899 During the Walmart GeForce Week Sale",
          "Grab this gaming PC for under $1,900", "Launch Week Deals Hit Latest Macs", "Best Prime Day laptop offers"]))
vv = [art(f"Verge story {i} unique{i} zebra{i}", f"lede{i} " * 40, "The Verge" if i % 2 else "The Verge AI",
          url=f"https://www.theverge.com/{i}", score=10 - i, kw=1) for i in range(4)]
_m, y_ = F.classify(vv + [dict(a) for a in pool()])
check("classify: maybe cap is per OUTLET (The Verge + The Verge AI share 2 slots)",
      sum(1 for a in y_ if "theverge" in a["url"]) <= F.MAX_PER_SOURCE_MAYBE, [a["source"] for a in y_])

got_ = Z._drop_meta_sentences("Nikon confirmed the Z9 II. It ships in 2027. The provided excerpt does not include pricing.")
check("summarize: sentence ending in a year/version is not glued to a meta one", got_ == "Nikon confirmed the Z9 II. It ships in 2027.", got_)
check("promo: price NEWS is not a promo", not any(F._PROMO_RE.search(t) for t in
      ["BYD undercuts the Tesla Model 3 starting at under $29,000", "Bitcoin drops to $60,000"]))
check("same_story: stop-word-only title overlap is not a match", not A.same_story(art("It is over", "a " * 50, "X1"), art("Is it over?", "b " * 50, "X2")))
g1 = art("Introducing GPT-5.5", "OpenAI introduces GPT-5.5 with better reasoning " * 5, "OpenAI Blog", score=9)
g2 = art("Introducing Qwen1.5", "Alibaba introduces Qwen1.5 open weights family " * 5, "Qwen Blog", score=3)
dd_ = F.deduplicate([dict(g1), dict(g2)])
tw_ = [t for a in dd_ for t in a.get("_twins", [])]
check("title path: 'Introducing X' ~ 'Introducing Y' is not the same story (no twin at all)", not tw_ and len(dd_) == 2, tw_)
syn1 = art("Anthropic ships Opus upgrade", "Anthropic released an upgraded Claude Opus model on Tuesday with a million-token context window, lower prices for API customers and a new tool-use mode aimed at coding agents. " * 2, "The Verge", score=9)
syn2 = dict(syn1, title="Claude gets million-token context", source="Techmeme", url="https://techmeme.com/x", _score=6)
d2_ = F.deduplicate([dict(syn1), dict(syn2)])
check("deduplicate: syndicated lede under an unrelated headline clusters (min-hash blocking)", len(d2_) == 1 and d2_[0]["_num_sources"] == 2, [(a["title"], a["_num_sources"]) for a in d2_])

_sc = "Apple has pushed its Siri overhaul to 2027, according to people familiar with the plan, after internal testing found the new assistant still unreliable. " * 2
check("same_story: syndicated lede wins over the sequel rule (delays/postpones)",
      A.same_story(art("Apple delays Siri overhaul to 2027", _sc, "The Verge"), art("Apple postpones Siri overhaul to 2027", _sc, "Engadget")))
check("summarize: 'available content' is news, not meta",
      Z._drop_meta_sentences("Netflix did not say when the available content library will grow in Asia. It launches today.")
      == "Netflix did not say when the available content library will grow in Asia. It launches today.")

_bl = "Apple today seeded a new beta of iOS 27.1 to developers for testing purposes, one week after releasing the previous beta with bug fixes and new features. " * 2
check("same_story: same-outlet sequel with a template lede is vetoed",
      not A.same_story(art("Apple Seeds Second Beta of iOS 27.1 to Developers", _bl, "MacRumors"), art("Apple Seeds Third Beta of iOS 27.1 to Developers", _bl, "MacRumors")))
check("same_story: cross-outlet identical lede overrides the sequel veto",
      A.same_story(art("Apple Seeds Second Beta of iOS 27.1 to Developers", _bl, "MacRumors"), art("Apple Seeds Third Beta of iOS 27.1 to Developers", _bl, "9to5Mac")))

check("_slots: Title Case name swap counts only if the body capitalises it",
      A._slot_conflict_t(A._slots("Anthropic Releases Claude Opus 5.5", None, "Claude Opus 5.5 is here"),
                         A._slots("Anthropic Releases Claude Sonnet 5.5", None, "Claude Sonnet 5.5 is here"))
      and not A._slot_conflict_t(A._slots("Claude Code Deletes Files", None, "It deleted files"),
                                 A._slots("Claude Code Erases Files", None, "It erased files")))

check("same_story: Title Case sequel split via story_features (body passed through)",
      not A.same_story(art("Anthropic Launches Claude Opus 5.5", "Claude Opus 5.5 is Anthropic's new model with better coding and reasoning for API customers. " * 3, "MacRumors"),
                       art("Anthropic Launches Claude Sonnet 5.5", "Claude Sonnet 5.5 is Anthropic's new model with better coding and reasoning for API customers. " * 3, "9to5Mac")))

# ── seen ledger + hold-back ───────────────────────────────────────────────────
SEEN = os.path.join(DATA, "seen_articles.json")
shown = art("Meta's Muse agent beats ChatGPT in App Store downloads",
            "Meta's Muse agent app overtook ChatGPT in US App Store downloads this week, according to Appfigures data, after Meta pushed the agent to Instagram users. " * 2, "TechCrunch")
if os.path.exists(SEEN):
    os.remove(SEEN)
S.save_seen_articles([shown], [], [])
led = json.load(open(SEEN))
check("save_seen_articles: entry carries url + story words", led[0].get("url") and isinstance(led[0].get("story"), list) and led[0]["story"], led[0])
para = art("Muse beats ChatGPT in downloads as Meta pushes agent to Instagram",
           "Meta's Muse agent beat ChatGPT in App Store downloads this week per Appfigures, helped by Meta pushing the agent to Instagram users. " * 2, "Slashdot")
def held(age_days, ttl=None):
    e = dict(led[0], ts=NOW - age_days * 86400)
    if ttl is not None:
        e["ttl_days"] = ttl
    json.dump([e], open(SEEN, "w"))
    rs = []
    F.load_seen_hashes(rs, F.HOLD_BACK_DAYS)
    _m, _o, hb = F.match_articles([dict(para)], {"topics": ["ChatGPT"]}, set(), rs)
    return hb == 1
check("hold-back: paraphrase of a story shown 3 days (+1 min) ago is held", held(3 + 60 / 86400))
check("hold-back: a re-telling is held for the article's whole eligible life (6 days)", held(6))
check("hold-back: ... but not after the window (8 days)", not held(8))
check("hold-back: an expired-TTL twin still holds its story back", held(2.5, ttl=2))
follow = art("Australia fines OpenAI A$50 million over health portal breach",
             "Australia's privacy regulator fined OpenAI A$50 million on Friday and ordered an independent audit, weeks after the company's rogue model hacked an Australian government website. Albanese welcomed the penalty; OpenAI said it would appeal the ruling in federal court. " * 2, "Reuters")
orig_ab = dict(AUS1, content=AUS1["content"] * 2)
def held_f(age_days):
    S.save_seen_articles  # noqa
    e = {"title": orig_ab["title"], "url": orig_ab["url"], "source": orig_ab["source"],
         "story": sorted(A.story_tokens(orig_ab)), "hashes": ["x"], "hash": "x",
         "ts": NOW - age_days * 86400}
    json.dump([e], open(SEEN, "w"))
    rs = []
    F.load_seen_hashes(rs, F.HOLD_BACK_DAYS)
    _m, _o, hb = F.match_articles([dict(follow)], {"topics": ["OpenAI"]}, set(), rs)
    return hb == 1
why = A.same_story(follow, orig_ab)
check("hold-back fixture: follow-up matches only on content", why and why.startswith("content"), why)
check("hold-back: content-only follow-up held the next run", held_f(1 + 60 / 86400))
check("hold-back: ... but released after one run", not held_f(2))
S.save_seen_articles([], [], [dict(para, title="twin", url="https://t.com/x")])
tw = [e for e in json.load(open(SEEN)) if e.get("title") == "twin"]
S.save_seen_articles([], [], [dict(para, title="strong twin", url="https://t.com/strong", _strong=True)])
st_ = [e for e in json.load(open(SEEN)) if e.get("title") == "strong twin"]
check("url/title-matched (strong) twin kept the full 7-day window", st_ and st_[0]["ttl_days"] == S.MAX_AGE_DAYS, st_)
check("send-time twin keeps the short window", tw and tw[0]["ttl_days"] == S.DEDUPED_TWIN_TTL_DAYS, tw)
S.save_seen_articles([], [], [dict(para, title="cluster twin", url="https://t.com/ct", _strong=False)])
ct_ = [e for e in json.load(open(SEEN)) if e.get("title") == "cluster twin"]
check("fetch-side cluster twin (direct match) gets the full 7-day window", ct_ and ct_[0]["ttl_days"] == S.MAX_AGE_DAYS, ct_)
check("re-telling hold-back covers the whole eligible life", F.HOLD_BACK_DAYS >= F.MAX_ARTICLE_AGE_DAYS)

rel = art("OpenAI launches GPT-6.5 with agent mode", "OpenAI launched GPT-6.5 on Monday with a new agent mode that can operate a browser, alongside faster responses and lower API prices for developers building on its platform. " * 2, "TechCrunch", score=24, kw=4)
repeat = art("GPT-6.5 now live in ChatGPT with agent mode", "OpenAI's GPT-6.5 is now live in ChatGPT with a new agent mode that can operate a browser, alongside faster responses and lower API prices for developers. " * 2, "9to5Mac", score=30, kw=7)
pulls = art("OpenAI pulls GPT-6.5 agent mode after it leaked user files", "OpenAI pulled GPT-6.5's agent mode on Thursday after the browser agent leaked user files to third-party sites; the company launched GPT-6.5 on Monday with lower API prices. " * 2, "The Verge", score=18, kw=4)
json.dump([{"title": rel["title"], "url": rel["url"], "source": rel["source"], "hashes": ["r"], "hash": "r",
            "story": sorted(A.story_tokens(rel)), "ts": NOW - 2 * 86400}], open(SEEN, "w"))
rs = []
F.load_seen_hashes(rs, F.HOLD_BACK_DAYS)
mm, _o, hb = F.match_articles([dict(repeat), dict(pulls)], {"topics": ["OpenAI", "GPT-6.5", "agent"]}, set(), rs)
dd = F.deduplicate(mm)
check("hold-back: the repeat is held, the new development it clusters with survives",
      any("pulls" in a["title"] for a in dd) and not any("now live" in a["title"] for a in dd),
      ([a["title"] for a in dd], [a["title"] for a in mm]))
# lap-4: a weak-held follow-up must not be recorded as a twin of a live item it matches
hands = art("OpenAI pulls browser feature after user files leaked to third-party sites", "Check which user files were exposed: OpenAI pulled the browser feature Thursday after it leaked user files to third-party sites, the company confirmed in a support note. " * 2, "Engadget", score=27, kw=4)
json.dump([{"title": rel["title"], "url": rel["url"], "source": rel["source"], "hashes": ["r"], "hash": "r",
            "story": sorted(A.story_tokens(rel)), "ts": NOW - 0.9 * 86400}], open(SEEN, "w"))
rs = []
F.load_seen_hashes(rs, F.HOLD_BACK_DAYS)
mm2, _o, hb2 = F.match_articles([dict(hands), dict(pulls)], {"topics": ["OpenAI", "GPT-6.5", "agent"]}, set(), rs)
dd2 = F.deduplicate(mm2)
tw2 = [t["title"] for a in dd2 for t in a.get("_twins", [])]
check("hold-back fixture: follow-up weak-held, live item not held, and they match",
      hb2 == 1 and [a["title"] for a in mm2] == [hands["title"]] and A.same_story(hands, pulls), (hb2, [a["title"] for a in mm2]))
check("hold-back: a held follow-up is never recorded as another item's twin",
      not any("GPT-6.5 agent mode after" in t for t in tw2), tw2)
for a_, b_, want in [("OpenAI raises 40B in new round", "OpenAI raises 50B in new round", False),
                     ("Windows 11 gets new Start menu", "Windows 12 gets new Start menu", True),
                     # BOTH swapped tokens must be identifiers: 5.5 -> "new" is not a sequel
                     ("OpenAI ships GPT-6.5 update today", "OpenAI ships GPT-new update today", False),
                     ("Nvidia unveils RTX 5090 and 5080 cards", "Nvidia unveils RTX 5070 and 5060 GPUs", False),
                     ("Gemini 4 hands-on: faster, but still buggy", "Gemini 5 hands-on: faster, but still buggy", True),
                     ("Disrupting malicious uses of AI: October 2025", "Disrupting malicious uses of AI: June 2025", True),
                     ("Galaxy Watch Ultra Drops To $350 At Amazon", "Galaxy Watch Ultra Drops To $400 At Amazon", False),
                     ("Claude Code Deleted Files In 103 Seconds", "Claude Code Deleted Files In 100 Seconds", False),
                     ("iPhone 18 Pro Leak Shows Redesigned Camera Bar", "iPhone 18 Pro Leak Details Redesigned Camera Bar", False),
                     ("Axios reports OpenAI deal", "Bloomberg reports OpenAI deal", False),
                     ("OpenAI previews GPT-6 for researchers", "OpenAI previews GPT-7 for researchers", True),
                     ("Apple's iPhone 17 gets a price cut", "Apple's iPhone 18 gets a price cut", True),
                     ("Claude Code Deletes 48,000 Files In Seconds", "Claude Code Erases 48,000 Files In Seconds", False),
                     ("Apple Seeds Second Beta of iOS 27.1 to Developers", "Apple Seeds Third Beta of iOS 27.1 to Developers", True),
                     ("iOS 27.1 beta 2 now available", "iOS 27.1 beta 3 now available", True),
                     ("Apple may delay Siri", "Apple might delay Siri", False),
                     ("Claude Code deleted 48,000 files", "Claude Code deleted 48,218 files", False),
                     ("Nvidia acquires Mistral for $50B", "Nvidia buys Mistral for $50B", False),
                     ("OpenAI valued at $380B in new round", "OpenAI valued at $350B in new round", False),
                     ("RTX 5090 Super review", "RTX 5080 Super review", True),
                     # Title Case without a body: capitals are no evidence (see the body test)
                     ("Anthropic Releases Claude Opus 5.5", "Anthropic Releases Claude Sonnet 5.5", False)]:
    check(f"_slot_conflict({a_[:24]}… vs …) == {want}", A._slot_conflict(a_, b_) == want)
_pr = "Anthropic today announced its newest model, available now in the API and in Claude apps, with improved coding, stronger reasoning and lower prices for enterprise customers. "
check("same_story: sequels with swapped name/number are NOT the same story",
      not A.same_story(art("Anthropic releases Claude Opus 5.5", _pr.replace("newest model", "newest model, Claude Opus 5.5,") * 2, "The Verge"),
                       art("Anthropic releases Claude Sonnet 5.5", _pr.replace("newest model", "newest model, Claude Sonnet 5.5,") * 2, "Ars Technica"))
      and not A.same_story(art("iOS 27.0.1 With Bug Fixes Now Available", "Apple today released iOS 27.0.1 with bug fixes " * 5, "MacRumors"),
                           art("iOS 27.0.2 With Bug Fixes Now Available", "Apple today released iOS 27.0.2 with bug fixes " * 5, "MacRumors")))

# lap-5: weak hold only against a story recorded BEFORE the candidate was published
late = dict(pulls, published=time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(NOW - 2 * 3600)))
e_after = {"title": rel["title"], "url": rel["url"], "source": rel["source"],
           "_story": sorted(A.story_tokens(rel)), "_age_s": 3600.0}        # recorded 1h ago
e_before = dict(e_after, _age_s=5 * 3600.0)                                 # recorded 5h ago
check("hold-back: weak tier ignores a sibling recorded AFTER the candidate was published",
      F._hold_grade(late, e_after) == "echo" and F._hold_grade(late, e_before) == "weak",
      (F._hold_grade(late, e_after), F._hold_grade(late, e_before)))
# lap-6: an old weak match ("echo") is NOT penalised as representative
echo_ = dict(pulls, _score=30.0)
low_ = dict(art("OpenAI pulls GPT-6.5 agent mode, reports say", pulls["content"], "Techmeme"), _score=4.0)
dd3 = F.deduplicate([echo_, low_])
check("deduplicate: the higher-ranked member leads (echoes are not de-preferred)",
      len(dd3) == 1 and dd3[0]["title"] == echo_["title"], [(a["title"], a["_score"]) for a in dd3])
# lap-5: held re-tellings are exported and recorded
ho = []
json.dump([{"title": rel["title"], "url": rel["url"], "source": rel["source"], "hashes": ["r"], "hash": "r",
            "story": sorted(A.story_tokens(rel)), "ts": NOW - 2 * 86400}], open(SEEN, "w"))
rs = []
F.load_seen_hashes(rs, F.HOLD_BACK_DAYS)
F.match_articles([dict(repeat)], {"topics": ["OpenAI", "GPT-6.5"]}, set(), rs, ho)
check("match_articles: a held re-telling is exported for recording", [a["title"] for a in ho] == [repeat["title"]], ho)

# lap-6: the held re-telling pipeline end to end (fetch export -> summarize -> send ledger)
check("match_articles: a WEAK hold is not exported as a re-telling",
      (lambda ho_: (F.match_articles([dict(late)], {"topics": ["OpenAI", "GPT-6.5"]}, set(),
                                     [dict(e_before)], ho_), ho_)[1])([]) == [])
mm_e, _o, _h = F.match_articles([dict(late)], {"topics": ["OpenAI", "GPT-6.5"]}, set(), [dict(e_after)])
check("match_articles: an old weak match is not held", len(mm_e) == 1 and _h == 0, (mm_e, _h))
import asyncio  # noqa: E402
json.dump({"total_scanned": 1, "skipped": 0, "matched_and_dropped": 0, "must_read": [],
           "maybe_relevant": [], "held_retellings": [{"title": "held copy", "url": "https://h/1",
           "source": "Hx", "_story": ["a"], "_strong": True}]}, open(os.path.join(DATA, "articles.json"), "w"))
_orr, _osa = Z.resolve_hermes_runtime, Z.summarize_all
Z.resolve_hermes_runtime = lambda: {"model": "m", "api_mode": "chat_completions", "base_url": "b"}
async def _noop(arts, rt):
    return arts
Z.summarize_all = _noop
asyncio.run(Z.main())
Z.resolve_hermes_runtime, Z.summarize_all = _orr, _osa
sm_ = json.load(open(os.path.join(DATA, "articles_summarized.json")))
check("summarize: passes held_retellings through", sm_.get("held_retellings") and sm_["held_retellings"][0]["title"] == "held copy", sm_)

# lap-7: a Title Case sequel is not held against a (body-less) ledger entry
_ob = "Anthropic today released Claude Opus 5.5, its most capable model, with better coding, stronger reasoning and lower prices for API customers and Claude apps. " * 2
_sb = "Anthropic today released Claude Sonnet 5.5, a faster model that beats Claude Opus 5.5 on coding, with lower prices for API customers and Claude apps. " * 2
opus = art("Anthropic Launches Claude Opus 5.5", _ob, "MacRumors")
sonnet = art("Anthropic Launches Claude Sonnet 5.5", _sb, "9to5Mac")
e_opus = {"title": opus["title"], "url": opus["url"], "source": opus["source"],
          "_story": sorted(A.story_tokens(opus)), "_age_s": 86400.0, "_ts": NOW - 86400}
check("hold-back: Title Case sequel vs ledger entry is not a re-telling",
      F._hold_grade(sonnet, e_opus) is None and not A.same_story(sonnet, opus),
      (F._hold_grade(sonnet, e_opus), A.same_story(sonnet, opus)))

# lap-8: stored identifier flags make the hold-back symmetric (alias re-telling held)
_od = "OpenAI's ChatGPT was down for hours on Tuesday for millions of users worldwide, the company said, blaming a faulty configuration change in its API gateway. " * 2
down1 = art("OpenAI Down For Hours After Configuration Error", _od, "The Verge")
down2 = art("ChatGPT Down For Hours After Configuration Error", "ChatGPT, OpenAI's chatbot, was down for hours on Tuesday for millions of users, after a faulty configuration change in its API gateway; service is now restored, OpenAI said. " * 2, "Engadget")
if os.path.exists(SEEN):
    os.remove(SEEN)
S.save_seen_articles([down1], [], [])
led_ = json.load(open(SEEN))
check("ledger entry stores title identifier flags (slots)", isinstance(led_[0].get("slots"), list) and led_[0]["slots"], led_[0].get("slots"))
for e_ in led_:
    e_["ts"] = NOW - 86400
json.dump(led_, open(SEEN, "w"))
rs = []
F.load_seen_hashes(rs, F.HOLD_BACK_DAYS)
_m, _o, hb_ = F.match_articles([dict(down2)], {"topics": ["OpenAI", "ChatGPT"]}, set(), rs)
# Known limit (documented): when both bodies name both aliases, a headline alias swap
# is indistinguishable from a sequel, so it is split. What the stored flags guarantee is
# CONSISTENCY — tomorrow's hold-back agrees with same-day clustering.
check("hold-back agrees with same-day matching (stored own-body flags)",
      (hb_ == 1) == bool(A.same_story(down1, down2)), (hb_, A.same_story(down1, down2)))
check("alias fixture: ledes in the re-telling band, below the 0.90 veto skip",
      F.HOLD_BACK_RETELL_CONTENT <= A.content_similarity(down1, down2) < A.SLOT_VETO_MAX_CONTENT, A.content_similarity(down1, down2))

# lap-9: a Title Case TWIN of a shown launch must not hold tomorrow's sequel
_lb = "Anthropic today launched Claude Opus 5.5 with better coding and reasoning, available now in the API and in Claude apps for all paid plans. " * 2
L1 = art("Anthropic Launches Claude Opus 5.5 With Better Coding", _lb, "The Verge", score=20)
L2 = art("Anthropic Launches Claude Opus 5.5 With Better Coding", _lb, "Engadget", score=10)
dd9 = F.deduplicate([dict(L1), dict(L2)])
if os.path.exists(SEEN):
    os.remove(SEEN)
# record the TWIN alone (the identical-title shown copy would share its identity hash):
# the question is whether the twin's stored flags were judged with its own body
S.save_seen_articles([], [], dd9[0].get("_twins", []))
led9 = json.load(open(SEEN))
for e_ in led9:
    e_["ts"] = NOW - 86400
json.dump(led9, open(SEEN, "w"))
rs = []
F.load_seen_hashes(rs, F.HOLD_BACK_DAYS)
seq9 = art("Anthropic Launches Claude Sonnet 5.5 With Better Coding",
           "Anthropic today launched Claude Sonnet 5.5, a faster sibling of Claude Opus 5.5, with better coding for API customers and Claude apps. " * 2, "TechCrunch")
_m, _o, hb9 = F.match_articles([dict(seq9)], {"topics": ["Anthropic", "Claude"]}, set(), rs)
check("hold-back: a Title Case twin of a shown launch doesn't hold its sequel",
      len(led9) == 1 and hb9 == 0, (len(led9), hb9, [e.get("slots") for e in led9]))

# 1.12 brief quality (Sep 28 brief)
old_pub = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(NOW - 6 * 86400))
llm36 = art("llm 0.36", "Release: llm 0.36 New OpenAI models: gpt-6-sol for GPT-6 Sol. " * 5, "Simon Willison", score=6.7, kw=3, pub=old_pub)
check("must-read: a 6-day-old item is never must-read", not F._must_read_eligible(dict(llm36, _num_sources=1)))
check("must-read: a weak (score < floor) item is never must-read",
      not F._must_read_eligible(art("OpenAI Claude LLM note", "x " * 150, "Simon Willison", score=6.0, kw=3, n=1)))
check("must-read: fresh + strong still qualifies",
      F._must_read_eligible(art("OpenAI Claude LLM news", "x " * 150, "The Decoder", score=18.0, kw=3, n=1)))
mr_few, may_few = F.classify([dict(llm36, _num_sources=1)] + [art(f"Zeta story {i} q{i}", f"w{i} " * 150, f"O{i}", score=12.0 - i, kw=1) for i in range(6)])
check("classify: fewer than 3 must-reads when fewer qualify (rest go to maybe)", len(mr_few) == 0 and len(may_few) == 5, (len(mr_few), len(may_few)))
fresh_ = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(NOW - 3600))
cands = [dict(art("Amazon Just Knocked $900 Off Apple's Latest 16-Inch MacBook Pro", "Amazon has cut $900 off the MacBook Pro with Apple M5 Max. " * 5, "MacRumors", pub=fresh_), tier="A"),
         dict(art("Googlebooks might be the real deal", "Hi, friends! Welcome to Installer No. 145, your guide to the best Apple and Google stuff. " * 5, "The Verge", pub=fresh_), tier="A"),
         dict(art("Apple ships macOS 27.1 with Claude and OpenAI and LLM and GPU and Gemini features", "Apple Claude OpenAI LLM GPU Gemini " * 20, "9to5Mac", pub=fresh_), tier="B")]
mq, _o, _h = F.match_articles([dict(c) for c in cands], {"topics": ["Apple", "Google", "Claude", "OpenAI", "LLM", "GPU", "Gemini"]}, set(), [])
by = {a["source"]: a for a in mq}
check("match_articles: deals/promos are excluded", "MacRumors" not in by, list(by))
check("match_articles: newsletters/roundups score half", by.get("The Verge", {}).get("_roundup") is True, by.get("The Verge"))
check("match_articles: keyword hits capped at 3 in the score", by.get("9to5Mac", {}).get("_score") == 3 * 2 * 3, by.get("9to5Mac", {}).get("_score"))
rd = [dict(art(t_, f"r{i} " * 150, "Reddit r/LocalLLaMA", url=f"https://www.reddit.com/r/LocalLLaMA/{i}", score=20 - i, kw=1), tier="C")
      for i, t_ in enumerate(["Quantized Qwen on a 3090 benchmarks", "My homelab inference rack build", "Which tokenizer handles Korean best"])]
check("reddit fixture: the three posts are distinct stories", F.deduplicate([dict(x) for x in rd]).__len__() == 3)
_mr, _may = F.classify(rd + [art(f"Other story {i} zz{i}", f"o{i} " * 150, f"Out{i}", score=5 - i * 0.1, kw=1) for i in range(5)])
check("classify: at most one Reddit post per brief", sum(1 for a in _may if "reddit" in a["url"]) == 1, [a["source"] for a in _may])
check("summary: a good summary that opens like a short lede is kept",
      not S._is_content_copy("OpenAI and Anthropic are investigating tens of thousands of incidents. OpenAI has paused training of its most capable models while it investigates.",
                             "OpenAI and Anthropic are investigating tens of thousands of incidents in which their AI agents independently hacked websites."))
check("summary: a verbatim copy of the lede is still caught",
      S._is_content_copy("OpenAI and Anthropic are investigating tens of thousands of incidents in which their AI agents",
                         "OpenAI and Anthropic are investigating tens of thousands of incidents in which their AI agents independently hacked websites."))
ex_ = Z._extractive_fallback("Hi, friends! Welcome to Installer No. 145, your guide to the best stuff. This week, I found a great laptop. It is fast.", "maybe_relevant")
check("extractive: no greeting, no cut at 'No.'", not ex_.startswith("Hi") and "No." not in ex_.split()[-1:], ex_)
check("summarize: token budget leaves room for reasoning", min(Z.MAX_TOKENS.values()) >= 4000)

check("roundup: 'this week in' / 'podcast' in a LEDE is news, not a roundup",
      not F._ROUNDUP_RE.search("U.S. government urges Supreme Court to reverse Apple contempt ruling")
      and not F._ROUNDUP_LEDE_RE.search("The U.S. government filed a brief this week in the Apple vs. Epic case"))
check("guide: evergreen buying guides are demoted", F._GUIDE_RE.search("Looking for your next Fujifilm lens? These top 12 primes")
      and F._GUIDE_RE.search("Best iPhone 18 Pro cases") and not F._GUIDE_RE.search("Apple's best quarter ever"))
check("families: synonyms count once", F.topic_families(["LLM", "language model", "OpenAI", "ChatGPT"]) == {"llm", "openai"})
ob = F.deduplicate([art("Apple hit with $5.7 billion verdict over Taptic Engine patent", "A jury ordered Apple to pay $5.7 billion over Taptic Engine patents, the largest verdict this year. " * 3, s_, score=9.0, kw=1)
                    for s_ in ("9to5Mac", "MacRumors", "The Verge")])
check("outlet bonus: a 3-outlet story scores +50%", len(ob) == 1 and abs(ob[0]["_score"] - 13.5) < 1e-6, [(a["_num_sources"], a["_score"]) for a in ob])
check("must-read floor ignores reader-preference scaling",
      F._must_read_eligible(art("OpenAI Claude LLM news", "x " * 150, "The Decoder", score=12 * 0.83, kw=3, n=1) | {"_pref": 0.83}))
ai_ = [art(f"AI story {i} vv{i}", f"a{i} " * 150, f"AIO{i}", score=15 - i, kw=2) | {"_families": ["llm"]} for i in range(6)]
ap_ = art("Apple ships new iPad", "b " * 150, "AppleO", score=8.0, kw=1) | {"_families": ["apple"]}
_m6, _y6 = F.classify(ai_ + [ap_])
check("diversity: at most 3 single-area AI stories in maybe when a decent non-AI story exists",
      sum(1 for a in _y6 if a.get("_families") == ["llm"]) <= F.MAYBE_MAX_PER_AREA + 1 and any(a is ap_ for a in _y6)
      and len(_y6) == 5 and _y6.index(ap_) < 4,
      [a["title"] for a in _y6])
_UNFX = json.loads(r'''{"un": {"title": "OpenAI agents tried to ‘bruteforce’ a UN website", "content": "The United Nations logo on a gate outside the UN headquarters in New York. | AFP via Getty Images Security researcher Rowan Howard-Jones says that OpenAI agents scanned the UN Conference on Trade and Development's (UNCTAD) statistics site over 16,000 times between April and June . While the incident doesn't quite rise to the level of the Hugging Face hack , or the recent attacks on US government s", "source": "The Verge AI", "url": "https://www.theverge.com/ai-artificial-intelligence/1001178/openai-agents-bruteforce-un-website"}, "entry": {"title": "Rogue OpenAI Agents Posted 53 User-Uploaded Images Onto the Internet, Accessed US Government Websites", "url": "https://slashdot.org/story/26/09/26/0328247/rogue-openai-agents-posted-53-user-uploaded-images-onto-the-internet-accessed-us-government-websites?utm_source=rss1.0mainlinkanon&utm_medium=feed", "source": "Slashdot", "story": ["53", "agent", "ai", "data", "discovered", "environment", "even", "hosting", "image", "included", "into", "link", "listed", "model", "openai", "posted", "public", "publicly", "research", "site", "still", "then", "those", "training", "uploaded", "user", "wer", "weren", "while"], "slots": [["rogue", false], ["openai", true], ["agent", false], ["posted", false], ["53", false], ["user", false], ["uploaded", false], ["image", false], ["onto", false], ["the", false], ["internet", false], ["accessed", false], ["us", false], ["government", false], ["website", false]]}}''')  # real Sep 28 pair
un = dict(_UNFX["un"], published=time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(NOW - 3600)))
_E = _UNFX["entry"]
e_un = {"title": _E["title"], "url": _E["url"], "source": _E["source"], "_story": _E["story"],
        "_age_s": 3600.0, "_ts": NOW - 7200, "_slots": tuple(tuple(t) for t in (_E["slots"] or []))}
check("hold-back: a distinct development on the weakest match is not held",
      F._hold_grade(un, e_un) == "echo", (A.same_story(un, e_un), F._hold_grade(un, e_un)))

# lap-2 gate: pin the wiring of the 1.12 scoring pieces
fresh_ = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(NOW - 3600))
syn_ = dict(art("LLM news", "LLM language model foundation model frontier model OpenAI ChatGPT " * 10, "The Decoder", pub=fresh_), tier="B")
g_ = dict(art("Looking for your next Fujifilm lens? These top 12 primes", "Fujifilm lenses mirrorless camera " * 20, "Fuji Rumors", pub=fresh_), tier="B")
mw, _o, _h = F.match_articles([dict(syn_), dict(g_)], {"topics": ["LLM", "language model", "foundation model", "frontier model", "OpenAI", "ChatGPT", "Fujifilm", "mirrorless camera"]}, set(), [])
bw = {a["source"]: a for a in mw}
check("match_articles: keyword count is the FAMILY count (6 synonyms -> 2 families)",
      bw["The Decoder"]["_interest_kw_count"] == 2 and bw["The Decoder"]["_families"] == ["llm", "openai"], bw["The Decoder"].get("_families"))
check("match_articles: a guide is demoted (x0.5) in the score", bw["Fuji Rumors"].get("_roundup") is True
      and abs(bw["Fuji Rumors"]["_score"] - 1 * 2 * 3 * 0.5) < 1e-6, bw["Fuji Rumors"].get("_score"))
check("guide: an OS version number is not a listicle count",
      not F._GUIDE_RE.search("TestFlight App Gets New iOS 27 Features") and F._GUIDE_RE.search("watchOS 27: 12 New Things to Try"))
low_ap = art("Apple minor note", "c " * 150, "AppleLow", score=5.0, kw=1) | {"_families": ["apple"]}
_m7, _y7 = F.classify([dict(a) for a in ai_] + [low_ap])
check("diversity: an other-area story below the threshold does not displace AI stories",
      sum(1 for a in _y7 if a.get("_families") == ["llm"]) == 5, [a["title"] for a in _y7])
# a waiting other-area story blocked by its outlet cap must not leave the section short
va = [art(f"Verge AI story {i} vx{i}", f"v{i} " * 150, "The Verge AI", url=f"https://www.theverge.com/ai/{i}", score=20 - i, kw=2) | {"_families": ["llm"]} for i in range(2)]
vap = art("Verge Apple story", "p " * 150, "The Verge", url="https://www.theverge.com/apple/1", score=8.0, kw=1) | {"_families": ["apple"]}
rest = [art(f"AI other {i} ox{i}", f"q{i} " * 150, f"AIX{i}", score=15 - i, kw=2) | {"_families": ["llm"]} for i in range(6)]
_m8, _y8 = F.classify(va + [vap] + rest)
check("diversity: a waiting story blocked by its outlet cap doesn't leave maybe short", len(_y8) == 5, [a["title"] for a in _y8])

rp_ = [art(f"Reddit Apple post {t_}", f"k{t_} " * 150, "Reddit r/LocalLLaMA", url=f"https://www.reddit.com/r/x/{t_}", score=9.0 - t_ * 0.1, kw=1) | {"_families": ["apple"], "tier": "C"}
       for t_ in range(3)]
_m9, _y9 = F.classify([dict(a) for a in ai_] + rp_)
check("diversity: never leaves maybe short when other-area stories are outlet-capped", len(_y9) == 5, [a["title"] for a in _y9])
check("guide: buyer's guides / 'Here are the N best' / 'The Top N' are demoted",
      all(F._GUIDE_RE.search(t) for t in ["These are the best MacBook deals right now: September 2026 Buyer's Guide",
                                          "Here are the 7 best iPhone cases", "The Top 38 Best Simpsons Episodes"]))

# ── Discord delivery ─────────────────────────────────────────────────────────
import requests  # noqa: E402
from feeds import FEEDS as _FEEDS  # noqa: E402
FEEDS_NAME = _FEEDS[0][0]
class R:
    def __init__(s, code, body): s.status_code, s._b, s.text, s.headers = code, body, json.dumps(body), {}
    def json(s): return s._b
calls = []
def fake_post(url, headers=None, json=None, timeout=None):
    calls.append(json)
    if len(calls) == 1:
        raise requests.exceptions.ReadTimeout("lost response")
    return R(200, {"id": "1"})
orig_post, orig_sleep = S.requests.post, S.time.sleep
S.requests.post, S.time.sleep = fake_post, (lambda s: None)
S._SEND_DEADLINE[0] = time.monotonic() + 60
S.post_message("u", {}, {"content": "hi", "nonce": "mb1", "enforce_nonce": True}, fatal=False)
check("post_message: retry resends the SAME nonce", len(calls) == 2 and calls[0]["nonce"] == calls[1]["nonce"] == "mb1", calls)

sent = []
S.DISCORD_BOT_TOKEN = "x"
S.requests.post = lambda url, headers=None, json=None, timeout=None: (sent.append(json), R(200, {"id": str(len(sent))}))[1]
os.remove(SEEN)
json.dump({FEEDS_NAME: {"consecutive_failures": 9, "url": "u"}}, open(os.path.join(DATA, "feed_health.json"), "w"))
shown_art = art("a1 shown", "c" * 300, "S1")
shown_art["_twins"] = [{"title": "a1 sibling from S2", "url": "https://s2.com/sib", "source": "S2", "_story": ["x"]}]
S.send_messages([], [shown_art], 10, 5, 0, [])
nonces = [p.get("nonce") for p in sent]
check("send_messages: feed-health notice sent with its own nonce", any(str(n).endswith("hn") for n in nonces), nonces)
S.send_messages([], [dict(shown_art, title="a2 shown", url="https://s1.com/a2")], 10, 5, 0, [],
                [{"title": "held copy", "url": "https://h/1", "source": "Hx", "_story": ["a"], "_strong": True}])
check("send_messages: held re-tellings recorded with the full window when delivered",
      any(e.get("title") == "held copy" and e.get("ttl_days") == S.MAX_AGE_DAYS for e in json.load(open(SEEN))))
check("send_messages: cluster twins of a delivered article recorded",
      any(e.get("title") == "a1 sibling from S2" and e.get("ttl_days") == S.DEDUPED_TWIN_TTL_DAYS
          for e in json.load(open(SEEN))))
os.remove(os.path.join(DATA, "feed_health.json"))
check("send_messages: every message has a unique nonce with enforce_nonce", nonces and all(nonces) and len(set(nonces)) == len(nonces)
      and all(p.get("enforce_nonce") for p in sent), nonces)
S.requests.post = lambda url, headers=None, json=None, timeout=None: R(403, {"code": 50013, "message": "Missing Permissions"})
try:
    S.send_messages([], [], 10, 10, 0, [], [{"title": "held copy 2", "url": "https://h/2", "source": "Hx", "_strong": True}])
    code = 0
except SystemExit as e:
    code = e.code
check("send_messages: empty day + header 403 exits non-zero", code == 1, code)
check("send_messages: nothing delivered -> held re-tellings NOT recorded",
      not any(e.get("title") == "held copy 2" for e in (json.load(open(SEEN)) if os.path.exists(SEEN) else [])))
cnt = []
def post_2nd_fails(url, headers=None, json=None, timeout=None):
    cnt.append(1)
    return R(200, {"id": "1"}) if len(cnt) == 1 else R(403, {"code": 50013, "message": "no"})
S.requests.post = post_2nd_fails
try:
    S.send_messages([], [], 10, 10, 0, [])
    code = 0
except SystemExit as e:
    code = e.code
check("send_messages: empty day, header ok but placeholder fails -> non-zero", code == 1, code)
S.requests.post, S.time.sleep = orig_post, orig_sleep

# ── summarize ─────────────────────────────────────────────────────────────────
out = Z._drop_meta_sentences("Adobe brings Lightroom to Gemini. Users can edit photos, though the provided excerpt did not include a launch date. It follows a deal.")
check("summarize: meta sentence about the excerpt dropped", "excerpt" not in out and out.startswith("Adobe") and out.endswith("deal."), out)
check("summarize: meta clause trimmed, rest of sentence kept", "Users can edit photos." in out, out)
for keep in ["Apple released an excerpt of Tim Cook's memoir. It sold well.",
             "The model translates source text into 40 languages.",
             "A code snippet leaked. B.",
             "Penguin shared the excerpt with reviewers ahead of launch, though sales figures were not released.",
             "Penguin shared the excerpt with reviewers but did not include the ending.",
             "Tim Cook wrote a memoir. The excerpt describes his early years at Compaq. It is out now.",
             "Apple said the text provided to regulators was redacted.",
             "Google, though criticized, released the excerpt from the book."]:
    check(f"summarize: legit sentence kept ({keep[:30]}…)", Z._drop_meta_sentences(keep) == keep, Z._drop_meta_sentences(keep))
ml = Z._drop_meta_sentences("Line one.\nLine two without period\nThe excerpt lacks details.")
check("summarize: unpunctuated lines survive", "Line two without period" in ml, ml)
for src, want in [("The U.S. Commerce Department order was not detailed in the provided excerpt. Anthropic complied.", "Anthropic complied."),
                  ("1. Apple shipped it.\n2. The provided excerpt omits pricing.", "1. Apple shipped it."),
                  ("The provided text does not say when it ships.", "The provided text does not say when it ships.")]:
    got_ = Z._drop_meta_sentences(src)
    check(f"summarize: abbreviations/list numbers/never-empty ({src[:28]}…)", got_ == want, got_)

# ── fetch: Accept-Encoding + --dry-run ────────────────────────────────────────
seen_hdr = {}
def fake_rb(method, deadline, **kw):
    seen_hdr.update(kw.get("headers", {}))
    raise requests.RequestException("stop")
orig_rb = F.request_bounded
F.request_bounded = fake_rb
F._FETCH_DEADLINE[0] = time.monotonic() + 5
F.fetch_feed("X", "https://x/feed", "B")
F.request_bounded = orig_rb
check("fetch_feed: Accept-Encoding pinned to gzip, deflate", seen_hdr.get("Accept-Encoding") == "gzip, deflate", seen_hdr)

HEALTH = os.path.join(DATA, "feed_health.json")
if os.path.exists(HEALTH):
    os.remove(HEALTH)
orig_fa = F.fetch_all_feeds
F.fetch_all_feeds = lambda: ([art("OpenAI Claude LLM story", "OpenAI Claude LLM " * 30, "S")],
                             {"S": {"url": "u", "ok": True, "status": "ok", "count": 1, "usable": 1}})
argv = sys.argv
sys.argv = ["fetch_rss.py", "--dry-run"]
F.main()
sys.argv = argv
F.fetch_all_feeds = orig_fa
check("fetch_rss --dry-run: feed_health.json not written", not os.path.exists(HEALTH))

# ── feed_doctor ───────────────────────────────────────────────────────────────
open(HEALTH, "wb").write(b"\xff\xfe garbage")
check("feed_doctor.load_health: truncated file -> {}", D.load_health() == {})
json.dump(["not", "a", "dict"], open(HEALTH, "w"))
check("feed_doctor.load_health: non-dict -> {}", D.load_health() == {})
from feeds import FEEDS  # noqa: E402
fname = FEEDS[0][0]
json.dump({fname: {"consecutive_failures": "0", "consecutive_zero_yield": "5"}}, open(HEALTH, "w"))
got = {}
orig_diag = D.diagnose
D.diagnose = lambda targets, topics: (got.setdefault("t", targets), [])[1]
sys.argv = ["feed_doctor.py"]
D.main()
sys.argv = argv
D.diagnose = orig_diag
check("feed_doctor: default mode includes zero-yield feeds (string counters OK)",
      got.get("t") and got["t"][0][0] == fname, got)

# ── wrapper lock ──────────────────────────────────────────────────────────────
# MB_WRAPPER=<path> tests a staged wrapper; otherwise the dev layout, then the live one.
cand = [p_ for p_ in (os.environ.get("MB_WRAPPER"),
                      os.path.join(os.path.dirname(HERE), "morning_brief_run.py"),
                      os.path.join(REAL_HOME, ".hermes", "scripts", "morning_brief_run.py"))
        if p_]
W = None
for c in cand:
    if os.path.exists(c):
        spec = importlib.util.spec_from_file_location("mbrun", c)
        W = importlib.util.module_from_spec(spec)
        sys.argv = ["morning_brief_run.py", "--dry-run"]
        spec.loader.exec_module(W)
        sys.argv = argv
        if hasattr(W, "_acquire_lock"):
            break
        W = None
if W is None:
    print("SKIP wrapper lock checks — no 1.10 wrapper found (set MB_WRAPPER=<staged path>)")
else:
    W.HERMES_HOME = os.path.join(TMP, ".hermes")
    h1 = W._acquire_lock()
    try:
        W._acquire_lock()
        code = 0
    except SystemExit as e:
        code = e.code
    check("wrapper: a concurrent --dry-run gives up at once", code == 1, code)
    _orig_sleep = W.time.sleep
    W.DRY, W.LOCK_WAIT_SECONDS, W.time.sleep = False, 0.1, (lambda s: None)
    failed = []
    W.fail = lambda step, reason: (failed.append((step, reason)), sys.exit(1))
    try:
        W._acquire_lock()
    except SystemExit:
        pass
    check("wrapper: a real run still locked out after the wait ANNOUNCES via fail()",
          failed and failed[0][0] == "lock", failed)
    W.time.sleep = _orig_sleep
    h1.close()
    W.HERMES_HOME = "/proc/definitely/not/writable"
    check("wrapper: unusable lock file -> run continues unlocked", W._acquire_lock() is None)
    spec.loader.exec_module(W)
    # A step that starts late gets only the budget that is left, so fail() can still post.
    W._T0 = time.monotonic() - (W.WATCHDOG_SECONDS - W.NOTIFY_RESERVE_SECONDS - 50)
    seen_to = {}
    W.subprocess.run = lambda cmd, **kw: (seen_to.update(t=kw.get("timeout")),
                                          types.SimpleNamespace(returncode=0))[1]
    W.step("send_brief.py")
    check("wrapper: a late step's timeout is capped by the remaining watchdog budget",
          seen_to.get("t") is not None and seen_to["t"] <= 50.5, seen_to)
    # 1.13.1: summarize.py runs under the first interpreter that can import Hermes' config
    # (Hermes v0.22.0 needs ruamel.yaml, which the system python lacks); fetch / send stay on INTERP.
    _real_run = W.subprocess.run
    probed, cmds = [], []
    def fake_run(cmd, **kw):
        if "-c" in cmd:
            probed.append(cmd[0])
            return types.SimpleNamespace(returncode=0 if cmd[0] == "/hermes/py" else 1)
        cmds.append(cmd[0])
        return types.SimpleNamespace(returncode=0)
    W.subprocess.run = fake_run
    W._hermes_interpreters = lambda: ["/sys/py", "/hermes/py", "/other/py"]
    W._T0 = time.monotonic()
    check("wrapper: summarize interpreter = the first that imports Hermes' config",
          W._summarize_interp() == "/hermes/py" and probed == ["/sys/py", "/hermes/py"], probed)
    W.step("summarize.py"); W.step("fetch_rss.py"); W.step("send_brief.py")
    check("wrapper: only summarize.py moves; fetch / send stay on the system python",
          cmds == ["/hermes/py", W.INTERP, W.INTERP], cmds)
    W._hermes_interpreters = lambda: ["/sys/py"]
    check("wrapper: no interpreter imports Hermes → INTERP (summarize reports the error)", W._summarize_interp() == W.INTERP)
    W.subprocess.run = _real_run
    spec.loader.exec_module(W)
    ints = W._hermes_interpreters()
    check("wrapper: candidates start with its own interpreter, include INTERP, no duplicates",
          ints and ints[0] == sys.executable and W.INTERP in ints and len(ints) == len(set(ints)), ints)

# ── feed_doctor.probe against a local server ─────────────────────────────────
import http.server, threading  # noqa: E402
FEED = (b"<?xml version='1.0'?><rss version='2.0'><channel><title>t</title>"
        + b"".join(b"<item><title>T%d</title><link>https://x/%d</link><description>"
                   % (i, i) + (b"word " * 60) + b"</description></item>" for i in range(5))
        + b"</channel></rss>")
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.send_header("Content-Type", "application/rss+xml")
        self.end_headers(); self.wfile.write(FEED)
    def log_message(self, *a): pass
try:
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
except OSError as e:
    srv = None
    print(f"SKIP feed_doctor.probe — can't bind a local test server here ({e})")
if srv:
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    for k in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        os.environ.pop(k, None)
    os.environ["NO_PROXY"] = "127.0.0.1"
    r = D.probe(f"http://127.0.0.1:{srv.server_port}/feed")
    check("feed_doctor.probe: bounded fetch + pipeline extraction", r.get("ok") and r.get("usable") == 5, r)
    srv.shutdown()

# ── reader ratings (feedback.py, 1.11) ───────────────────────────────────────
import feedback as FB  # noqa: E402
def rx(*pairs):         # [(emoji, count, me)] → Discord reaction summary
    return [{"emoji": {"id": None, "name": e}, "count": c, "me": me} for e, c, me in pairs]
check("ratings: 👍 → +1", FB.rating_from_reactions(rx(("👍", 1, False))) == 1)
check("ratings: 👎 with a skin tone → -1", FB.rating_from_reactions(rx(("👎🏽", 1, False))) == -1)
check("ratings: 😐 is not a rating (no reaction = neutral)", FB.rating_from_reactions(rx(("😐", 1, False))) is None)
check("ratings: 👍 + 👎 together cancel", FB.rating_from_reactions(rx(("👍", 1, False), ("👎", 1, False))) is None)
check("ratings: the bot's own reaction doesn't count", FB.rating_from_reactions(rx(("👍", 1, True))) is None)
check("ratings: other emoji / junk ignored",
      FB.rating_from_reactions(rx(("🎉", 3, False))) is None and FB.rating_from_reactions("x") is None
      and FB.rating_from_reactions([{"emoji": None, "count": "a"}]) is None)

# build_messages: one maybe article per message, header once, separator after the last only
mr_a = [art(f"must {i} Claude launch", "c" * 300, f"M{i}") for i in range(2)]
may_a = [art(f"maybe {i} OpenAI thing", "c" * 300, f"S{i}") for i in range(5)]
msgs = S.build_messages(mr_a, may_a, 10, 1, 0)
may_msgs = [m for m in msgs if m[0].startswith("maybe")]
check("build_messages: one message per maybe article, each carrying exactly it",
      len(may_msgs) == 5 and all(len(c) == 1 and c[0] is a for (_l, _t, c), a in zip(may_msgs, may_a)),
      [m[0] for m in msgs])
check("build_messages: '### Maybe relevant' exactly once, on the first maybe message",
      sum("### Maybe relevant" in m[1] for m in msgs) == 1 and may_msgs[0][1].startswith("### Maybe relevant"))
check("build_messages: section separator after the last maybe only",
      may_msgs[-1][1].endswith(S.SECTION_SEP) and not any(m[1].endswith(S.SECTION_SEP) for m in may_msgs[:-1]))
check("build_messages: footer carries the rating hint", "rate with 👍 👎" in msgs[-1][1], msgs[-1][1])

# send_messages → manifest with the delivered message ids
BR = os.path.join(DATA, "briefs")
import shutil  # noqa: E402
shutil.rmtree(BR, ignore_errors=True)           # earlier send tests recorded manifests too
sent2 = []
def fake_ok(url, headers=None, json=None, timeout=None):
    sent2.append(json)
    return R(200, {"id": str(1000 + len(sent2))})
S.requests.post, S.time.sleep = fake_ok, (lambda s: None)
m1 = dict(art("Claude gets a new memory feature", "c" * 300, "The Verge", url="https://www.theverge.com/a1"), _topics=["Claude"])
m2 = dict(art("OpenAI lawsuit over training data", "c" * 300, "Ars Technica", url="https://arstechnica.com/a2"), _topics=["OpenAI"])
S.send_messages([m1], [m2], 10, 1, 0, [])
man = FB.load_manifest(FB._resolve_date("today"))
ids = [a["message_id"] for a in man["articles"]]
check("manifest: one entry per delivered article, with its own message id and section",
      len(man["articles"]) == 2 and ids == ["1002", "1003"]
      and [a["section"] for a in man["articles"]] == ["must_read", "maybe_relevant"]
      and man["channel_id"] == S.CHANNEL_ID, man)
check("manifest: outlet / topics / terms recorded",
      man["articles"][0]["outlet"] == "theverge.com" and man["articles"][0]["topics"] == ["Claude"]
      and "memory" in man["articles"][0]["terms"] and "claude" not in man["articles"][0]["terms"], man["articles"][0])
S.requests.post = lambda url, headers=None, json=None, timeout=None: R(200, {"nope": 1})
S.send_messages([], [dict(m2, title="x unknown id", url="https://z.com/u")], 10, 1, 0, [])
check("manifest: an 'unknown' message id is not recorded", len(FB.load_manifest(FB._resolve_date("today"))["articles"]) == 2)
orig_rb2 = FB.record_brief
FB.record_brief = lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
S.requests.post = fake_ok
try:
    S.send_messages([], [dict(m2, title="y manifest fails", url="https://z.com/y")], 10, 1, 0, [])
    code = 0
except SystemExit as e:
    code = e.code
check("manifest: a write failure never fails the send (ledger still written)",
      code == 0 and any(e.get("title") == "y manifest fails" for e in json.load(open(SEEN))), code)
FB.record_brief = orig_rb2
S.requests.post, S.time.sleep = orig_post, orig_sleep

# sync_reactions: add / change / remove, manual wins, failures keep ratings, dry run doesn't save
os.environ["DISCORD_BOT_TOKEN"] = "t"
pages = {"reactions": {"1002": rx(("👎", 1, False)), "1003": rx(("👍", 1, False))}, "fail": False}
def fake_get(url, token, params, deadline):
    if pages["fail"]:
        raise RuntimeError("Discord HTTP 503")
    return [{"id": mid, "reactions": r} for mid, r in pages["reactions"].items()] + [{"id": "999999"}]
orig_get = FB._discord_get
FB._discord_get = fake_get
st, rt = FB.sync_reactions()
check("sync: new ratings from reactions", rt.get("1002", {}).get("rating") == -1 and rt.get("1003", {}).get("rating") == 1, st)
check("sync: saved to feedback.json", FB.load_ratings().keys() == {"1002", "1003"})
pages["reactions"]["1003"] = rx(("👎", 1, False))
st, rt = FB.sync_reactions(persist=False)
check("sync: dry run sees the change but doesn't save",
      rt["1003"]["rating"] == -1 and FB.load_ratings()["1003"]["rating"] == 1, st)
pages["reactions"]["1003"] = []
FB.sync_reactions()
check("sync: a removed reaction removes the rating", "1003" not in FB.load_ratings())
check("CLI: rate is a manual override", FB.main(["rate", "today", "2", "1", "--note", "more like this"]) == 0
      and FB.load_ratings()["1003"]["via"] == "manual" and FB.load_ratings()["1003"]["note"] == "more like this")
pages["reactions"]["1003"] = rx(("👎", 1, False))
FB.sync_reactions()
check("sync: never overwrites a manual rating", FB.load_ratings()["1003"]["rating"] == 1)
check("CLI: unrate drops the manual rating", FB.main(["unrate", "today", "2"]) == 0 and "1003" not in FB.load_ratings())
check("CLI: bad input is rejected", FB.main(["rate", "today", "9", "1"]) == 1 and FB.main(["rate", "today", "1", "5"]) == 2
      and FB.main(["rate", "today", "1", "0"]) == 2)
pages["fail"] = True
st, rt = FB.sync_reactions()
check("sync: a failed fetch keeps every stored rating", FB.load_ratings().get("1002", {}).get("rating") == -1 and "503" in st, st)
FB._discord_get = orig_get
os.environ.pop("DISCORD_BOT_TOKEN")

# Model
check("model: no ratings → no model (ranking unchanged)", FB.build_model({}) is None)
def rated(title, outlet, topics, r, days=1, url=None):
    a = {"title": title, "url": url or f"https://{outlet}/{abs(hash(title))}", "source": outlet,
         "outlet": outlet, "topics": topics, "terms": FB.article_terms({"title": title}, topics),
         "story": [], "sent_ts": NOW - days * 86400, "rating": r}
    return a
bad4 = {str(i): rated(f"Decoder piece {i} about stuff", "the-decoder.com", ["OpenAI"], -1) for i in range(4)}
mdl = FB.Model(bad4)
cand = {"title": "Something else entirely", "url": "https://the-decoder.com/x", "source": "The Decoder", "_topics": ["LLM"]}
mult, why = mdl.preference(cand)
check("model: repeatedly disliked outlet ranks lower", 0.5 <= mult < 0.9 and "outlet" in why, (mult, why))
one = {"1": rated("The Verge AI memory feature ships", "theverge.com", ["AI agent"], -1)}
m1_, _ = FB.Model(one).preference({"title": "The Verge AI memory feature ships again", "url": "https://theverge.com/q",
                                   "source": "The Verge", "_topics": ["AI agent"]})
check("model: ONE 👎 is moderate — outlet+topic+shared words stay above the must-read floor",
      FB.PREF_MUST_READ_FLOOR < m1_ < 1.0, m1_)
five = {str(i): rated(f"Verge story {i} x{i}", "theverge.com", ["LLM"], -1) for i in range(5)}
m5_, _ = FB.Model(five).preference({"title": "Other", "url": "https://theverge.com/o", "source": "V", "_topics": ["LLM"]})
check("model: consistent 👎s on an outlet+topic reach the must-read floor", m5_ < FB.PREF_MUST_READ_FLOOR, m5_)
check("ratings: malformed entries are dropped, not fatal",
      (json.dump({"ratings": {"a": {"rating": 1, "outlet": ["x"]}, "b": {"rating": True},
                              "c": {"rating": 1, "title": None}, "d": {"rating": -1, "outlet": "ok.com"}}},
                 open(FB.FEEDBACK_FILE, "w")), list(FB.load_ratings()))[1] == ["d"])
mult_other, _ = mdl.preference({"title": "Unrelated", "url": "https://arstechnica.com/y", "source": "Ars", "_topics": ["LLM"]})
check("model: an unrelated outlet/topic is untouched", mult_other == 1.0, mult_other)
many = {str(i): rated(f"great {i}", "simonwillison.net", ["LLM"], 1) for i in range(40)}
mm_, _ = FB.Model(many).preference({"title": "x", "url": "https://simonwillison.net/z", "source": "S", "_topics": ["LLM"]})
check("model: multiplier capped at MULT_MAX", mm_ == FB.MULT_MAX, mm_)
mixed = dict(bad4, **{f"n{i}": rated(f"good {i}", "the-decoder.com", [], 1) for i in range(2)})
mult_n, _ = FB.Model(mixed).preference(cand)
check("model: 👍s on the same outlet offset its 👎s", mult < mult_n, (mult, mult_n))
old = {str(i): rated(f"Decoder piece {i}", "the-decoder.com", [], -1, days=200) for i in range(4)}
mult_old, _ = FB.Model(old).preference(cand)
check("model: old ratings decay", mult < mult_old < 1.0 and mult_old > 0.9, mult_old)
liked_story = {"s": dict(rated("Australian PM says OpenAI hacked government health website", "techxplore.com", [], 1),
                         story=sorted(A.story_tokens(AUS1)))}
fu, why_fu = FB.Model(liked_story).preference(dict(AUS2, _topics=["OpenAI"]))
check("model: a follow-up of a liked story is boosted", fu > 1.0 and "follow-up" in why_fu, (fu, why_fu))

# match_articles / classify integration
F.MUST = None
fresh = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(NOW - 3600))
c1 = {"title": "OpenAI and Anthropic ship LLM agents", "content": "OpenAI Anthropic LLM " + "x " * 200,
      "source": "The Decoder", "tier": "A", "url": "https://the-decoder.com/c1", "published": fresh}
base, _o, _h = F.match_articles([dict(c1)], {"topics": ["OpenAI", "Anthropic", "LLM"]}, set(), [])
adj, _o, _h = F.match_articles([dict(c1)], {"topics": ["OpenAI", "Anthropic", "LLM"]}, set(), [], None, mdl)
check("match_articles: records matched topic names", base[0]["_topics"] == ["Anthropic", "LLM", "OpenAI"], base[0].get("_topics"))
check("match_articles: no model → score and keys unchanged", "_pref" not in base[0])
check("match_articles: model multiplies the score and explains why",
      abs(adj[0]["_score"] - base[0]["_score"] * adj[0]["_pref"]) < 1e-6 and adj[0]["_pref"] < 1 and adj[0]["_pref_why"],
      (adj[0].get("_score"), adj[0].get("_pref")))
elig = dict(base[0], _num_sources=1)
check("classify: a strongly disliked article can't take a must-read slot",
      F._must_read_eligible(elig) and not F._must_read_eligible(dict(elig, _pref=0.6))
      and F._must_read_eligible(dict(elig, _pref=0.8)))
dis = dict(base[0], _num_sources=1, _score=99.0, _pref=0.6, title="Disliked OpenAI Anthropic LLM story", url="https://d.com/1")
ok_ = dict(base[0], _num_sources=1, _score=12.0, title="Fine Claude Apple GPU story", url="https://e.com/2", content="Claude Apple GPU " + "y " * 200)
mr_c, may_c = F.classify([dis, ok_])
check("classify: a disliked top scorer goes to maybe, not must-read",
      [a["url"] for a in mr_c] == ["https://e.com/2"] and [a["url"] for a in may_c] == ["https://d.com/1"],
      ([a["url"] for a in mr_c], [a["url"] for a in may_c]))
got_persist = []
orig_sync = FB.sync_reactions
FB.sync_reactions = lambda deadline=None, persist=True, now=None: (got_persist.append(persist), ("stub", {}))[1]
orig_fa, orig_argv = F.fetch_all_feeds, sys.argv
F.fetch_all_feeds = lambda: ([dict(c1)], {"X": {"ok": True, "usable": 1}})
sys.argv = ["fetch_rss.py", "--dry-run"]
try:
    F.main()
finally:
    F.fetch_all_feeds, sys.argv, FB.sync_reactions = orig_fa, orig_argv, orig_sync
check("fetch main: --dry-run syncs ratings without saving", got_persist == [False], got_persist)
class Boom:
    def __bool__(self): return True
    def preference(self, a, f=None): raise ValueError("bug")
bm, _o, _h = F.match_articles([dict(c1)], {"topics": ["OpenAI", "Anthropic", "LLM"]}, set(), [], None, Boom())
check("match_articles: a preference bug never drops the article", len(bm) == 1 and bm[0]["_score"] == base[0]["_score"])

# ── 1.13: deep same-story path + chained cluster members ──────────────────────
FX = {  # real feed items from the 2026-10-05 fetch, bodies trimmed to their first 1,000 chars
    'sc_verge': ('An AI couldn’t beat humans at StarCraft, so it decided to cheat',
            "StarSkirmish pits AI-made StarCraft-playing bots against one another, as well as against human-made bots. OpenAI's GPT-6 Astra and Claude Opus 5.5 were essentially tied as the best-performing AI-made bots, but they couldn't top Stardust, the top-rated human-made bot. On Friday, GPT was facing off against Claude and the human-created bot Pluto, but according to Kotaku , it couldn't quite get an edge. So it resorted to a tactic that is becoming alarmingly common for modern AI models - it broke the rules . GPT-6 Astra went and downloaded Stardust, and started running that instead of its own bot. GPT-6 Astra just cheated by downloading a copy … Read the full story at The Verge.",
            'The Verge AI', 'https://www.theverge.com/ai-artificial-intelligence/1004543/openai-gpt-cheat-starcraft'),
    'sc_pcg': ("An OpenAI model was caught trying to cheat at StarCraft, and of course it did it by stealing a human's work",
            "Commercial large-language models—and the companies that create them—have repeatedly been accused of stealing the work of humans. They've been lambasted for it by artists and sued for it by authors , while senior AI figures have even admitted in writing to mass-scale labour theft. But I haven't seen an example quite so brazen as an OpenAI model pilfering a human-created bot to try to win a round of StarCraft . As reported by Kotaku , the incident occurred during the ongoing botmatch StarSkirmish , in which human and AI-created bots battle one another in a perpetual cycle of warfare. According to the rules, the large-language models are given one hour to program a bot capable of playing as Protoss, which are then pitted against one another to see how they fare. On Friday, a three-way match was held between OpenAI's GPT-6 Astra, Anthropic's Claude Opus 5.5, and a human-created bot named Pluto. Apparently, Astra's bot was consistently struggling to ahead against the other two bots, so the ",
            'PC Gamer', 'https://www.pcgamer.com/software/ai/an-openai-model-was-caught-trying-to-cheat-at-starcraft-and-of-course-it-did-it-by-stealing-a-humans-work/'),
    'top': ('Top Stories: Apple Smart Home Launch on October 13, iPhone Duo Production Issues, and More',
            "Things are getting crazy in the Apple rumor world, as even though the iPhone 18 Pro and several other products have only just launched and the iPhone Duo is right around the corner, there's even more to come imminently. October 13 looks to be the date for an introduction of Apple's revamped smart home portfolio, including the home hub device we've been hearing about for ages. This week also saw word that Apple's supply chain is struggling to meet production yield goals for the iPhone Duo ahead of its launch, while iPhone 18 Pro owners are now getting a chance to put the devices through their paces, so read on below for all of the details! Top Stories Three New Apple Smart Home Products Coming on October 13 A couple of those leaked products above could be arriving very shortly, as Bloomberg 's Mark Gurman says Apple will be introducing a trio of new smart home products on Tuesday, October 13 . Apple will reportedly debut its long-rumored smart home hub product, as well as the new HomePo",
            'MacRumors', 'https://www.macrumors.com/2026/10/03/top-stories-apple-smart-home-launch-october-13/'),
    'homepad': ('Apple’s HomePad smart home hub launching on Oct 13 with iMac G4 design – Bloomberg',
            'Apple’s long-awaited smart home hub, which we’ve been referring to as the HomePad , is set to launch on October 13, according to Bloomberg . The device is said to feature a 6-inch display and a design inspired by the iMac G4, and will launch alongside a HomePod mini update with new colors and a new Apple TV 4K with Siri AI support … more…',
            '9to5Mac', 'https://9to5mac.com/2026/09/30/apples-homepad-smart-home-hub-launching-on-oct-13-bloomberg/'),
    'halide': ('Halide Camera App Takes Apple’s New Variable Aperture Even Further',
            'The new iPhone 18 Pro and Pro Max smartphones have been out in the wild for a couple of weeks, and the headline new feature is the variable-aperture main camera. Third-party camera apps are getting updates to take advantage, including the popular iPhone camera app Halide. [ Read More ]',
            'PetaPixel', 'https://petapixel.com/2026/10/02/halide-camera-app-takes-apples-new-variable-aperture-even-further/'),
    'fcc': ('Final Cut Camera updated with support for iPhone 18 Pro features, more',
            'Apple is rolling out a big update to Final Cut Camera today, the pro video app it first released for iPhone back in 2024. The update introduces a new design along with support for variable aperture on iPhone 18 Pro. more…',
            '9to5Mac', 'https://9to5mac.com/2026/09/29/final-cut-camera-iphone-18-pro-features/'),
    'fda_mr': ("Apple Announces 'Full Disk Access' Changes on macOS Due to AI Agents",
            'In a post on its developer news website today, Apple announced that it will be introducing "additional controls" for the "Full Disk Access" setting on macOS due to new risks posed by AI agents. This announcement comes amid the rise of always-on AI agents like Meta\'s Muse and OpenAI\'s Dots, which have prompted privacy concerns. "Some developers are using Full Disk Access in ways that could put users at risk, exposing everything on their systems—including files, mail, messages, and even browsing history—without users\' full knowledge and understanding," said Apple. "As AI agents become increasingly capable and autonomous, the risks associated with this level of access will grow substantially," it added. Here is the full text of Apple\'s announcement: We give developers powerful APIs to build incredible capabilities into their apps for Apple products, backed by a set of controls designed to protect users\' private data. Full Disk Access largely sidesteps these controls in order to allow back',
            'MacRumors', 'https://www.macrumors.com/2026/10/02/apple-announces-macos-full-disk-access-changes/'),
    'fda_tc': ('Apple says it’s tightening macOS ‘Full Disk Access’ controls due to new risks from AI agents',
            'Apple says it will add new controls around macOS’s Full Disk Access permission, warning that increasingly capable AI agents make broad access to users’ files, messages, mail, and browsing history riskier.',
            'TechCrunch', 'https://techcrunch.com/2026/10/02/apple-says-its-tightening-macos-full-disk-access-controls-due-to-new-risks-from-ai-agents/'),
    'fda_9': ('Apple says it’s tightening macOS privacy controls amid the rise of AI agents',
            'Amid the rapid rise in AI agents requesting “Full Disk Access” to your Mac, Apple says it is making changes to macOS to protect user privacy. While the company hasn’t shared specifics, Apple says users should understand what it means to give an app such “extraordinary” access to their Mac. more…',
            '9to5Mac', 'https://9to5mac.com/2026/10/02/apple-says-its-tightening-macos-privacy-controls-amid-the-rise-of-ai-agents/'),
}
def fx(k, **kw):
    t, c, s, u = FX[k]
    return dict(art(t, c, s, url=u), **kw)
SCV, SCP = fx("sc_verge"), fx("sc_pcg")
why = A.same_story(SCV, SCP)
check("deep: PC Gamer's commentary-led StarCraft story matches The Verge's past the lede",
      why and why.startswith("deep"), why)
fv, fp = A.story_features(SCV), A.story_features(SCP)
check("deep fixture: on the lede alone the pair does not match (the Oct 5 miss)",
      A.story_overlap_f(fv, fp)[1] < A.CONTENT_WEAK_THRESHOLD and A.same_story_f(fv[:8], fp[:8]) is None,
      (A.story_overlap_f(fv, fp), A.same_story_f(fv[:8], fp[:8])))
check("deep: a round-up sharing 'smart home' with the HomePad launch stays apart (0.117)",
      not A.same_story(fx("top"), fx("homepad")), A.same_story(fx("top"), fx("homepad")))
check("deep: one shared title word is not enough at any depth (Halide ~ Final Cut Camera, 0.23)",
      not A.same_story(fx("halide"), fx("fcc")), A.same_story(fx("halide"), fx("fcc")))
check("deep: never within one outlet",
      not A.same_story(dict(SCV, source="PC Gamer", url="https://www.pcgamer.com/x"), SCP))
SHARED = ("The benchmark suite covers coding, maths, long-context retrieval and agentic tool use, "
          "and the company published per-task scores alongside latency and price per million tokens "
          "for every configuration it tested, including the batch tier and the priority tier. ") * 3
OP = art("Claude Opus 5.5 review: fast and careful", "Opus is the larger model, aimed at hard coding work where it rarely gives up on a task. " * 4 + SHARED, "Outlet A")
SN = art("Claude Sonnet 5.5 review: fast and careful", "Sonnet is the mid-sized model, tuned for latency-sensitive chat where every token costs. " * 4 + SHARED, "Outlet B")
check("deep: the sequel veto still runs first (Opus 5.5 / Sonnet 5.5 sharing a spec sheet)",
      A.same_story(OP, SN) is None, A.same_story(OP, SN))
WK = dict(SN, title="We spent a week with Opus 5.5, and careful is the word")
check("deep fixture: a non-sequel headline on the same body matches past the lede",
      (A.same_story(OP, WK) or "").startswith("deep"), A.same_story(OP, WK))
ws = A.with_slots(fv, ())
check("with_slots replaces the sequel flags and keeps the deep words", ws[7] == () and ws[8] == fv[8] and len(ws) == len(fv))
DP = [dict(SCP), dict(SCV)]
A.annotate_story_tokens(DP)
check("annotate_story_tokens: sets _story_deep beyond the lede",
      isinstance(DP[0].get("_story_deep"), list) and set(DP[0]["_story"]) < set(DP[0]["_story_deep"]))
BP = "Read the full story and every update on our live blog, which we keep refreshed all day long."
bp = [art(f"Item {i} about {w}", f"{w} news opens here. " * 20 + BP, "Bp") for i, w in enumerate(("quartz", "falcon", "meadow"))]
A.annotate_story_tokens(bp)
check("annotate_story_tokens: per-source boilerplate is stripped from the deep window too",
      "blog" not in bp[0]["_story_deep"] and "quartz" in bp[0]["_story_deep"], bp[0]["_story_deep"])

# the deep words travel through the ledger into the hold-back
json.dump([], open(SEEN, "w"))
S.save_seen_articles([SCP], [], [])
e_ = json.load(open(SEEN))[0]
check("ledger: a shown article records its deep words",
      isinstance(e_.get("story_deep"), list) and len(e_["story_deep"]) > len(e_["story"]), e_.get("story_deep"))
e_["ts"] = NOW - 2 * 3600                        # shown 2 h before The Verge's copy was published
json.dump([e_], open(SEEN, "w"))
rs = []
F.load_seen_hashes(rs, F.HOLD_BACK_DAYS)
check("load_seen_hashes: the deep words reach the hold-back set", rs and rs[0].get("_story_deep") == e_["story_deep"])
check("hold-back: a deep re-telling published after the story was shown is held one run",
      F._hold_grade(SCV, rs[0]) == "weak", F._hold_grade(SCV, rs[0]))
old_e = {k: v for k, v in rs[0].items() if k != "_story_deep"}
check("hold-back: a pre-1.13 entry (no deep words) never matches on the deep path",
      F._hold_grade(SCV, old_e) is None, F._hold_grade(SCV, old_e))

# chained members: the Oct 3 "Full Disk Access" trio (9to5Mac matched only TechCrunch's copy)
MR_ = fx("fda_mr", score=30, kw=3)
TC_ = fx("fda_tc", score=20, kw=3)
NM_ = fx("fda_9", score=10, kw=3)
TRIO = [dict(MR_), dict(TC_), dict(NM_)]
A.annotate_story_tokens(TRIO)                    # as fetch_rss does before clustering
dd = F.deduplicate(TRIO)
check("deduplicate: the Oct 3 trio is one cluster led by MacRumors",
      len(dd) == 1 and dd[0]["source"] == "MacRumors", [(a["title"], a["source"]) for a in dd])
r0 = dd[0]
check("deduplicate: 9to5Mac (linked via TechCrunch) is chained, TechCrunch is the twin",
      [c["source"] for c in r0.get("_chained", [])] == ["9to5Mac"]
      and [t["source"] for t in r0["_twins"]] == ["TechCrunch"], (r0.get("_chained"), r0["_twins"]))
check("deduplicate: twins carry their deep words", isinstance(r0["_twins"][0].get("_story_deep"), list))
dd2 = F.deduplicate([dict(MR_), dict(TC_, source="Techmeme"), dict(NM_)])
check("deduplicate: a link through an aggregator item is not chained", dd2[0]["_chained"] == [], dd2[0].get("_chained"))
dd3 = F.deduplicate([dict(MR_), dict(TC_, _roundup=True), dict(NM_)])
check("deduplicate: ... nor a link through a round-up", dd3[0]["_chained"] == [], dd3[0].get("_chained"))
dd4 = F.deduplicate([dict(MR_), dict(TC_), dict(NM_, _roundup=True)])
check("deduplicate: ... and a round-up member is never chained", dd4[0]["_chained"] == [], dd4[0].get("_chained"))
solo = F.deduplicate([dict(MR_)])
check("deduplicate: a lone article has an empty _chained", solo[0]["_chained"] == [])
def _chain_of(member):
    trio = [dict(MR_), dict(TC_), dict(member)]
    A.annotate_story_tokens(trio)
    return F.deduplicate(trio)[0]["_chained"]
check("deduplicate: a member from the rep's OWN outlet is not chained (strict same-outlet rule)",
      _chain_of(dict(NM_, source="MacRumors", url="https://www.macrumors.com/2026/10/02/privacy")) == [])
check("deduplicate: an aggregator member is not chained",
      _chain_of(dict(NM_, source="Slashdot", url="https://apple.slashdot.org/story/1")) == [])
SPEC = ("The benchmark suite covers coding, maths, retrieval and agentic tool use, with "
        "per-task scores published alongside latency and pricing. ")
OPUS_R = art("Claude Opus 5.5 review: fast and careful", "Opus is the larger model, aimed at hard multi-file coding work where it rarely gives up, and it costs more. " + SPEC, "Outlet A", url="https://a.com/opus", score=30)
LAUNCH = art("Anthropic launches Claude Opus 5.5 and Claude Sonnet 5.5", "Anthropic released two models today, a flagship and a cheaper tier, rolling out to paid plans first and the API next week, with enterprise seats and regional data residency following later this quarter. " + SPEC, "Outlet C", url="https://c.com/launch", score=10)
SONNET_R = art("Claude Sonnet 5.5 review: fast and careful", "Sonnet is the mid-sized model, tuned for latency-sensitive chat and cheap agents in production apps, and it undercuts most rivals on throughput per dollar in our own testing this week. " + SPEC, "Outlet B", url="https://b.com/sonnet", score=20)
SQ = [dict(OPUS_R), dict(LAUNCH), dict(SONNET_R)]
A.annotate_story_tokens(SQ)
sq = F.deduplicate(SQ)
check("sequel fixture: Opus/Sonnet reviews link through the launch story",
      len(sq) == 1 and A.same_story(SQ[0], SQ[2]) is None and A.same_story(SQ[1], SQ[2]),
      [(a["title"], a.get("_chained")) for a in sq])
check("deduplicate: a SEQUEL of the rep is not chained (Sonnet 5.5 review via the launch story)",
      sq and sq[0]["_chained"] == [], sq and sq[0]["_chained"])
check("firm_story_f: the deep path is not a firm link", A.firm_story_f(fv, fp) is None and A.same_story_f(fv, fp))
# a real weak-band pair from the Oct 5 fetch: same game series, different stories (content@10%)
WEAK_A = art("I added October-appropriate zombies to Grand Theft Auto 5", "Los Santos Customs I'm modding GTA 5 every day in a quest to either transform it into a PC version of GTA 6 or break it entirely. Come back tomorrow to find out what new horrible thing I've done to Rockstar's baby, or check out what I got up to earlier . There is no more room in hell—the dead are at", "PC Gamer", url="https://www.pcgamer.com/games/grand-theft-auto/i-added-october-appropriate-zombies-to-grand-theft-auto-5/")
WEAK_B = art('Grand Theft Auto 6 may be the first GTA to have full-on "sex scenes", Hot Coffee not included', 'Grand Theft Auto 6 may feature actual sex scenes. The suggestion comes from an age-rating description for GTA 6 on the PlayStation Store, which states - if viewing the New Zealand or Australian versions of the Store - there are "sex scenes" in the game. Read more', "Eurogamer", url="https://www.eurogamer.net/gta-6-sex-scenes-pegi-esrb-ratings-boards")
wf = (A.story_features(WEAK_A), A.story_features(WEAK_B))
check("firm_story_f: the 8-12% lede band is not a firm link either (GTA 5 zombies ~ GTA 6 ratings)",
      (A.same_story_f(*wf) or "").startswith("content") and A.firm_story_f(*wf) is None,
      (A.same_story_f(*wf), A.story_overlap_f(*wf)))
check("firm_story_f: a title match is firm", A.firm_story_f(A.story_features(MR_), A.story_features(TC_)))
GTA_R = art('GTA 6 rating hints at the first full-on "sex scenes" in Grand Theft Auto', "An age-rating description for GTA 6 on the PlayStation Store in New Zealand and Australia lists \"sex scenes\", a first for Rockstar's series, which last courted controversy with the Hot Coffee mod twenty years ago.", "Kotaku", url="https://kotaku.com/gta-6-rating", score=9)
GTA = [dict(GTA_R), dict(WEAK_B, _score=5), dict(WEAK_A, _score=3)]
A.annotate_story_tokens(GTA)
gd = F.deduplicate(GTA)
check("deduplicate: a member reached only through the 8-12% band is clustered but NOT chained",
      len(gd) == 1 and A.same_story(GTA[0], GTA[2]) is None and gd[0]["_chained"] == [],
      [(a["title"][:40], a.get("_chained")) for a in gd])
check("deduplicate: nothing is chained under a round-up rep",
      F.deduplicate([dict(TRIO[0], _roundup=True), dict(TRIO[1]), dict(TRIO[2])])[0]["_chained"] == [])
def _scp_at(hours_before):
    return dict(SCP, published=time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(NOW - 3600 - hours_before * 3600)))
check("deep: 30 h apart still matches, 40 h apart does not (DEEP_MAX_PUB_GAP_HOURS 36)",
      (A.same_story(SCV, _scp_at(30)) or "").startswith("deep") and A.same_story(SCV, _scp_at(40)) is None,
      (A.same_story(SCV, _scp_at(30)), A.same_story(SCV, _scp_at(40))))
check("_pub_ts: a naive time is UTC (as fetch_rss.parse_published); garbage is None",
      A._pub_ts({"published": "2026-10-04T10:00:00"}) == A._pub_ts({"published": "2026-10-04T10:00:00+00:00"})
      and A._pub_ts({"published": "2026-10-04T10:00:00Z"}) == A._pub_ts({"published": "2026-10-04T10:00:00+00:00"})
      and A._pub_ts({"published": "soon"}) is None and A._pub_ts({"published": 5}) is None)
old_scp = dict(SCP, published=time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(NOW - 4 * 86400)))
check("deep: not across a long publication gap (a follow-up days later recaps the original)",
      A.same_story(SCV, old_scp) is None, A.same_story(SCV, old_scp))
check("deep: a ledger-like side with no publication date still compares",
      (A.same_story(SCV, {k: v for k, v in SCP.items() if k != "published"}) or "").startswith("deep"))

json.dump([], open(SEEN, "w"))
S.save_seen_articles([MR_], [], [dict(r0["_twins"][0])], r0["_chained"])
ch = [e for e in json.load(open(SEEN)) if e.get("kind") == "chained"]
check("ledger: a chained member is recorded by identity only, with the short ttl",
      len(ch) == 1 and ch[0]["ttl_days"] == S.CHAINED_TTL_DAYS and "story" not in ch[0]
      and "story_deep" not in ch[0] and ch[0]["hashes"] == A.article_hashes(NM_), ch)
json.dump([], open(SEEN, "w"))
S.save_seen_articles([MR_], [], [dict(TC_, _strong=True)], [{k: TC_[k] for k in ("title", "url", "source")}])
k_ = [e for e in json.load(open(SEEN)) if e["title"] == TC_["title"]]
check("ledger: an article that is both twin and chained keeps its twin entry",
      len(k_) == 1 and k_[0].get("kind") != "chained" and k_[0]["ttl_days"] == S.MAX_AGE_DAYS, k_)
def chained_at(age_days):
    json.dump([dict(ch[0], ts=NOW - age_days * 86400)], open(SEEN, "w"))
    rs_ = []
    hs = F.load_seen_hashes(rs_, F.HOLD_BACK_DAYS)
    return bool(set(ch[0]["hashes"]) & hs), rs_
in2, rs2 = chained_at(2)
in4, _ = chained_at(4)
check("reader: a chained entry suppresses its article for CHAINED_TTL_DAYS, then expires", in2 and not in4, (in2, in4))
check("writer: a chained entry keeps its own ttl (not the pre-1.10 no-story 7-day rule)",
      S._entry_ttl(ch[0]) == S.CHAINED_TTL_DAYS == min(S.CHAINED_TTL_DAYS, S.MAX_AGE_DAYS))
check("reader: a chained entry never joins the hold-back set", rs2 == [], rs2)
json.dump([dict(ch[0], ts=NOW - 86400)], open(SEEN, "w"))
mm_, _o, _hb = F.match_articles([dict(NM_)], {"topics": ["Apple"]}, F.load_seen_hashes(), [])
check("match_articles: the chained article itself is dropped as seen the next day", mm_ == [], [a["title"] for a in mm_])

sent2 = []
orig_post2 = S.requests.post
S.requests.post = lambda url, headers=None, json=None, timeout=None: (sent2.append(json), R(200, {"id": str(1000 + len(sent2))}))[1]
S._SEND_DEADLINE[0] = time.monotonic() + 60
json.dump([], open(SEEN, "w"))
S.send_messages([dict(r0)], [], 10, 5, 0, [])
S.requests.post = orig_post2
led_ = json.load(open(SEEN))
check("send_messages: chained members of a delivered article are recorded",
      any(e.get("kind") == "chained" and e.get("source") == "9to5Mac" for e in led_), [(e.get("source"), e.get("kind")) for e in led_])
S.requests.post = lambda url, headers=None, json=None, timeout=None: (sent2.append(json), R(200, {"id": str(2000 + len(sent2))}))[1]
S._SEND_DEADLINE[0] = time.monotonic() + 60
json.dump([], open(SEEN, "w"))
try:
    S.send_messages([dict(MR_, _chained=5, _twins="junk")], [], 10, 5, 0, [])
    bad_ok = True
except Exception as e_:                     # noqa: BLE001
    bad_ok = repr(e_)
S.requests.post = orig_post2
check("send_messages: a malformed _chained / _twins can't crash the post-delivery ledger write",
      bad_ok is True and any(e.get("title") == MR_["title"] for e in json.load(open(SEEN))), bad_ok)

bad = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(bad)}/{len(RESULTS)} passed" + (" — ALL PASS" if not bad else ""))
sys.exit(1 if bad else 0)
