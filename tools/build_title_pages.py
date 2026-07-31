#!/usr/bin/env python3
"""作品別の静的HTMLページを量産してロングテールSEOを獲得する。

- TMDBの人気映画/ドラマを取得し、各作品の詳細＋日本の配信先を事前取得
- 1作品 = 1静的ページ: title/{movie|tv}/{id}/index.html
  （配信先テキスト・構造化データ・OGP・canonicalを静的に埋め込む）
- ハブページ title/index.html で全作品へ内部リンク
- sitemap.xml をトップ＋ハブ＋全作品URLで再生成

使い方:
  python3 tools/build_title_pages.py [movie_pages] [tv_pages]
  （省略時は少数=テスト用。1ページ=20件。例: 15 10 → 映画300/ドラマ200）

配信先は日次で変わるため、週次などで再実行して再生成する想定。
"""
import json, os, sys, time, html, datetime, urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor

KEY = "aa9cb50c43ec1d77e920aa78e66e32e9"
BASE = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p/"
SITE = "https://dokomiru.tv"
ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15"
TODAY = datetime.date.today().isoformat()

TIER_LABEL = {"flatrate": "見放題", "free": "無料", "ads": "無料(広告つき)", "rent": "レンタル", "buy": "購入"}

# ====================================================================
# アフィリエイト設定（index.html の AFFILIATE_LINKS / EXTRA_AFFILIATE と対で管理。
# ASP提携承認後に {PLACEHOLDER} を実値へ差し替えると自動で有効化される。
# プレースホルダが残っている間はリンクを出さない＝壊れたリンクを出さない）
# キー = TMDB provider 名（dedupe_names 正規化後）。
# ====================================================================
AFFILIATE_LINKS = {
    "U-NEXT":             {"url": "https://ck.jp.ap.valuecommerce.com/servlet/referral?sid={VC_SID}&pid={VC_PID_UNEXT}", "label": "U-NEXTの無料トライアルで観る"},
    "Amazon Prime Video": {"url": "https://af.moshimo.com/af/c/click?a_id={MOSHIMO_A_ID}&p_id={MOSHIMO_P_ID_AMAZON}", "label": "Prime Videoを無料で試す"},
    "Hulu":               {"url": "https://h.accesstrade.net/sp/cc?rk={AT_RK_HULU}", "label": "Huluの無料トライアルで観る"},
    "Disney Plus":        {"url": "https://h.accesstrade.net/sp/cc?rk={AT_RK_DISNEY}", "label": "Disney+を試す"},
    "DMM TV":             {"url": "https://px.a8.net/svt/ejp?a8mat={A8MAT_DMMTV}", "label": "DMM TVを無料で試す"},
}
# ABEMA は TMDB 配信データに載らないため、作品個別の配信有無は主張せず
# 汎用のPR枠として掲載する（A8提携承認済み 2026-06-08）。
ABEMA_AD = {
    "url": "https://px.a8.net/svt/ejp?a8mat=4B5R01+EAEK7U+4EKC+5YRHE",
    "imp": "https://www17.a8.net/0.gif?a8mat=4B5R01+EAEK7U+4EKC+5YRHE",
    "label": "ABEMAプレミアムを無料で試す",
}


def aff_ready(conf):
    """提携IDが実値に差し替わっていれば True（{XXX} が残っていれば未設定＝無効）。"""
    import re
    return bool(conf) and not re.search(r"\{[A-Z0-9_]+\}", conf["url"])


# 比較表に載せる主要サービス（表示名, TMDB provider 名の候補）
MAJOR_SERVICES = [
    ("Netflix", ["Netflix"]),
    ("Amazonプライム", ["Amazon Prime Video", "Amazon Video"]),
    ("U-NEXT", ["U-NEXT"]),
    ("Disney+", ["Disney Plus"]),
    ("Hulu", ["Hulu"]),
    ("DMM TV", ["DMM TV"]),
]


def api(path, **params):
    params["api_key"] = KEY
    url = BASE + path + "?" + urllib.parse.urlencode(params)
    last = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except Exception as e:  # noqa
            last = e
            time.sleep(0.8 * (attempt + 1))
    raise last


def discover(media, pages):
    """人気順に作品IDを集める（ポスター有り・一定の投票数以上）。"""
    ids = []
    for p in range(1, pages + 1):
        params = {
            "language": "ja-JP", "sort_by": "popularity.desc", "watch_region": "JP",
            "include_adult": "false", "page": str(p),
            "vote_count.gte": "50",
            "with_watch_monetization_types": "flatrate|free|ads|rent|buy",
        }
        try:
            d = api("/discover/%s" % media, **params)
        except Exception:
            continue
        for r in d.get("results", []):
            if r.get("id") and r.get("poster_path"):
                ids.append(r["id"])
        time.sleep(0.04)
    return list(dict.fromkeys(ids))  # 重複除去（順序維持）


def trending(media, pages=2):
    """今週のトレンド作品ID（「作品名+配信」の検索需要が立ちやすい層）。"""
    ids = []
    for p in range(1, pages + 1):
        try:
            d = api("/trending/%s/week" % media, language="ja-JP", page=str(p))
        except Exception:
            continue
        for r in d.get("results", []):
            if r.get("id") and r.get("poster_path"):
                ids.append(r["id"])
        time.sleep(0.04)
    return list(dict.fromkeys(ids))


def fetch_one(media, mid):
    try:
        d = api("/%s/%s" % (media, mid), language="ja-JP", append_to_response="recommendations,credits")
        pv = api("/%s/%s/watch/providers" % (media, mid))
    except Exception:
        return None
    jp = ((pv.get("results") or {}).get("JP")) or {}
    return {"media": media, "detail": d, "jp": jp}


# --- 配信先の整形 ---
def dedupe_names(provs):
    seen, out = set(), []
    for p in provs or []:
        name = (p.get("provider_name") or "").replace(" Standard with Ads", "").replace(" with Ads", "").strip()
        if name and name not in seen:
            seen.add(name)
            out.append({"name": name, "logo": p.get("logo_path")})
    return out


def availability(jp):
    """tier->[{name,logo}] の辞書と、自然文の配信サマリを返す。"""
    tiers = {}
    for key in ("flatrate", "free", "ads", "rent", "buy"):
        names = dedupe_names(jp.get(key))
        if names:
            tiers[key] = names
    return tiers


def summary_sentence(title, tiers):
    flat = [n["name"] for n in tiers.get("flatrate", [])] + [n["name"] for n in tiers.get("free", [])] + [n["name"] for n in tiers.get("ads", [])]
    paid = [n["name"] for n in tiers.get("rent", [])] + [n["name"] for n in tiers.get("buy", [])]
    if flat:
        s = "『%s』は%sで見放題配信中です。" % (title, "・".join(dict.fromkeys(flat)))
        if paid:
            s += "レンタル・購入は%sで視聴できます。" % "・".join(dict.fromkeys(paid))
        return s
    if paid:
        return "『%s』は%sでレンタル・購入できます（2026年時点で見放題配信はありません）。" % (title, "・".join(dict.fromkeys(paid)))
    return "『%s』は、現在 日本の配信サービスでの配信情報が見つかりませんでした。" % title


# --- HTML生成 ---
CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Hiragino Kaku Gothic ProN","Noto Sans JP",sans-serif;
background:radial-gradient(1200px 600px at 50% -10%,rgba(124,92,255,.12),transparent 60%) ,#0b0d12;color:#f4f5fa;line-height:1.7}
a{color:inherit;text-decoration:none}
.wrap{max-width:680px;margin:0 auto;padding:0 16px 56px}
header{position:sticky;top:0;background:rgba(11,13,18,.78);backdrop-filter:blur(10px);border-bottom:1px solid #232834;z-index:5}
.bar{max-width:680px;margin:0 auto;padding:12px 16px;display:flex;align-items:center;gap:8px}
.logo{font-weight:800;font-size:1.15rem;background:linear-gradient(135deg,#7c5cff,#b14cff 55%,#ff5c8a);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
.hero{display:flex;gap:16px;margin:22px 0 8px}
.poster{width:120px;flex:0 0 120px;border-radius:14px;aspect-ratio:2/3;object-fit:cover;background:#1f242e}
.h-meta{min-width:0}
h1{font-size:1.32rem;font-weight:800;line-height:1.35;margin-bottom:8px}
.tags{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
.tag{font-size:.72rem;color:#98a0b3;border:1px solid #232834;border-radius:999px;padding:3px 9px}
.lead{margin:14px 0;color:#cfd3df;font-size:.96rem}
.card{background:#161a22;border:1px solid #232834;border-radius:16px;padding:16px;margin:14px 0}
.card h2{font-size:1rem;margin-bottom:12px}
.svc{display:flex;flex-wrap:wrap;gap:8px;margin:6px 0 14px}
.svc span{display:inline-flex;align-items:center;gap:7px;background:#1f242e;border:1px solid #2a2f3a;border-radius:10px;padding:6px 11px;font-size:.84rem}
.svc img{width:22px;height:22px;border-radius:5px;object-fit:cover}
.tier-label{font-size:.78rem;color:#98a0b3;font-weight:700;margin-bottom:4px}
.cta{display:block;text-align:center;background:linear-gradient(135deg,#7c5cff,#b14cff 55%,#ff5c8a);color:#fff;font-weight:800;
padding:15px;border-radius:14px;margin:18px 0;font-size:1rem;box-shadow:0 6px 20px rgba(124,92,255,.35)}
.overview{color:#cfd3df;font-size:.92rem;margin:10px 0}
.sub{font-size:.82rem;color:#98a0b3;font-weight:700;margin:24px 0 10px}
.rel{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.rel a{display:block}
.rel img{width:100%;border-radius:10px;aspect-ratio:2/3;object-fit:cover;background:#1f242e}
.rel p{font-size:.74rem;color:#cfd3df;margin-top:5px;line-height:1.3;
display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
footer{border-top:1px solid #232834;margin-top:34px;padding-top:16px;font-size:.74rem;color:#7a8193}
footer a{color:#98a0b3;text-decoration:underline}
.pr-note{font-size:.7rem;color:#7a8193;margin:12px 0 0}
.cta-aff{background:linear-gradient(135deg,#00b894,#0984e3);box-shadow:0 6px 20px rgba(9,132,227,.3);margin:12px 0}
.cta-abema{background:linear-gradient(135deg,#0eb87f,#0e9fb8);box-shadow:0 6px 20px rgba(14,184,127,.3);margin:12px 0 4px}
.ad-box .overview{margin-top:10px}
.ad-note{font-size:.68rem;color:#7a8193;border:1px solid #2a2f3a;display:inline-block;padding:1px 8px;border-radius:6px}
.cmp{width:100%;border-collapse:collapse;font-size:.88rem}
.cmp td{padding:9px 6px;border-bottom:1px solid #232834}
.cmp tr:last-child td{border-bottom:none}
.cmp .mk{font-weight:800;text-align:center;width:2.4em}
.cmp-note{font-size:.72rem;color:#7a8193;margin-top:10px}
.faq h2{font-size:.94rem}
""".strip()


def esc(s):
    return html.escape(str(s or ""), quote=True)


def render_page(item):
    media = item["media"]
    d = item["detail"]
    is_movie = media == "movie"
    title = d.get("title") if is_movie else d.get("name")
    title = title or d.get("original_title") or d.get("original_name") or "（タイトル不明）"
    date = (d.get("release_date") if is_movie else d.get("first_air_date")) or ""
    year = date[:4] if date else ""
    type_label = "映画" if is_movie else "ドラマ"
    poster = (IMG + "w342" + d["poster_path"]) if d.get("poster_path") else ""
    poster_og = (IMG + "w500" + d["poster_path"]) if d.get("poster_path") else SITE + "/assets/og.png"
    overview = (d.get("overview") or "").strip()
    genres = [g["name"] for g in (d.get("genres") or [])][:4]
    vote = d.get("vote_average") or 0
    vcount = d.get("vote_count") or 0
    runtime = d.get("runtime") if is_movie else ((d.get("episode_run_time") or [None])[0])

    tiers = availability(item["jp"])
    summ = summary_sentence(title, tiers)
    url = "%s/title/%s/%s/" % (SITE, media, d["id"])

    desc = "『%s』%sの配信はどこ？%s 日本のNetflix・Amazonプライム・U-NEXT・Disney+など見放題/レンタル/購入を最新チェック。" % (
        title, ("（%s年）" % year if year else ""), summ)
    desc = desc[:155]

    # 配信先カード
    if tiers:
        blocks = []
        for key in ("flatrate", "free", "ads", "rent", "buy"):
            if key not in tiers:
                continue
            chips = "".join(
                '<span>%s%s</span>' % (
                    ('<img loading="lazy" src="%sw45%s" alt="">' % (IMG, n["logo"]) if n.get("logo") else ""),
                    esc(n["name"]))
                for n in tiers[key])
            blocks.append('<p class="tier-label">%s</p><div class="svc">%s</div>' % (TIER_LABEL[key], chips))
        avail_html = "".join(blocks)
    else:
        avail_html = '<p class="overview">現在、日本の配信サービスでの配信情報が見つかりませんでした。配信が始まると、ここに表示されます。</p>'

    # --- アフィリCTA（配信中サービスのうち提携済みのみ・最大2本。未提携は出さない） ---
    all_names = []
    for key in ("flatrate", "free", "ads", "rent", "buy"):
        for n in tiers.get(key, []):
            if n["name"] not in all_names:
                all_names.append(n["name"])
    aff_ctas = []
    for name in all_names:
        conf = AFFILIATE_LINKS.get(name)
        if not aff_ready(conf):
            continue
        aff_ctas.append('<a class="cta cta-aff" href="%s" rel="noopener nofollow sponsored">%s</a>' % (
            esc(conf["url"]), esc(conf["label"])))
        if len(aff_ctas) >= 2:
            break
    aff_html = "".join(aff_ctas)

    # --- 主要サービス○×比較表（TMDB実データのみ。データに無いものは「配信情報なし」表記で断定しない） ---
    tier_of = {}
    for key in ("flatrate", "free", "ads"):
        for n in tiers.get(key, []):
            tier_of.setdefault(n["name"], "sub")
    for key in ("rent", "buy"):
        for n in tiers.get(key, []):
            tier_of.setdefault(n["name"], "paid")
    cmp_rows = []
    for label, cands in MAJOR_SERVICES:
        st = next((tier_of[c] for c in cands if c in tier_of), None)
        if st == "sub":
            mark, note = "◎", "見放題"
        elif st == "paid":
            mark, note = "○", "レンタル・購入"
        else:
            mark, note = "−", "配信情報なし"
        cmp_rows.append('<tr><td>%s</td><td class="mk">%s</td><td>%s</td></tr>' % (esc(label), mark, note))
    compare_html = ('<div class="card"><h2>📊 主要サービスの配信状況（%s時点）</h2>'
                    '<table class="cmp"><tbody>%s</tbody></table>'
                    '<p class="cmp-note">JustWatch由来のデータに基づく表示です。「配信情報なし」はデータ上見つからなかったことを示し、実際の配信有無は各公式サイトでご確認ください。</p>'
                    '</div>' % (TODAY, "".join(cmp_rows)))

    # --- キャスト・スタッフ（credits実データのみ） ---
    credits = d.get("credits") or {}
    cast_names = [c.get("name") for c in (credits.get("cast") or [])[:6] if c.get("name")]
    if is_movie:
        staff_label = "監督"
        staff_names = [c.get("name") for c in (credits.get("crew") or []) if c.get("job") == "Director" and c.get("name")][:2]
    else:
        staff_label = "制作"
        staff_names = [c.get("name") for c in (d.get("created_by") or []) if c.get("name")][:2]
    cast_bits = []
    if staff_names:
        cast_bits.append('<p class="overview"><strong>%s：</strong>%s</p>' % (staff_label, esc("、".join(staff_names))))
    if cast_names:
        cast_bits.append('<p class="overview"><strong>出演：</strong>%s</p>' % esc("、".join(cast_names)))
    cast_html = ('<p class="sub">キャスト・スタッフ</p>%s' % "".join(cast_bits)) if cast_bits else ""

    # --- FAQ（全て実データから機械生成。憶測の回答は書かない） ---
    netflix_st = tier_of.get("Netflix")
    if netflix_st == "sub":
        faq_a1 = "%s時点の配信データでは、Netflixで見放題配信されています。最新の配信状況はNetflix公式サイトでご確認ください。" % TODAY
    elif netflix_st == "paid":
        faq_a1 = "%s時点の配信データでは、Netflixでレンタル・購入形式の配信情報があります。最新の配信状況はNetflix公式サイトでご確認ください。" % TODAY
    else:
        faq_a1 = "%s時点の配信データでは、Netflixでの配信情報は見つかりませんでした。%s" % (TODAY, summ)
    flat_names = list(dict.fromkeys(
        [n["name"] for n in tiers.get("flatrate", [])] +
        [n["name"] for n in tiers.get("free", [])] +
        [n["name"] for n in tiers.get("ads", [])]))
    if flat_names:
        faq_a2 = "%sの見放題配信で視聴できます。サービスによっては無料トライアル期間が用意されている場合があります（実施状況・条件は各公式サイトでご確認ください）。" % "・".join(flat_names)
    else:
        faq_a2 = "%s時点では見放題配信の情報が見つかりませんでした。レンタル・購入対応のサービス、または今後の配信開始をご確認ください。" % TODAY
    faq_a3 = "本ページの配信情報は%s時点のJustWatch由来データに基づきます。配信状況は日々変わるため、最新・正確な情報は各サービスの公式サイトでご確認ください。" % TODAY
    faqs = [
        ("『%s』はNetflixで見られる？" % title, faq_a1),
        ("『%s』を無料で見る方法は？" % title, faq_a2),
        ("配信情報はいつ時点のもの？", faq_a3),
    ]
    faq_html = '<p class="sub">よくある質問</p>' + "".join(
        '<div class="card faq"><h2>Q. %s</h2><p class="overview">A. %s</p></div>' % (esc(q), esc(a))
        for q, a in faqs)
    faq_ld_json = json.dumps({
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [{"@type": "Question", "name": q,
                        "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in faqs],
    }, ensure_ascii=False)

    # --- ABEMA汎用PR枠（TMDBデータに載らないため作品個別の配信有無は主張しない） ---
    abema_html = ('<div class="card ad-box"><span class="ad-note">PR</span>'
                  '<p class="overview">アニメ・ドラマ・バラエティ・スポーツを配信するABEMAプレミアムには無料体験期間があります（実施状況・条件は公式サイトでご確認ください）。※本作品がABEMAで配信されているかは、ABEMA公式サイトでご確認ください。</p>'
                  '<a class="cta cta-abema" href="%s" rel="noopener nofollow sponsored">%s</a>'
                  '<img src="%s" width="1" height="1" alt="" style="border:0">'
                  '</div>' % (esc(ABEMA_AD["url"]), esc(ABEMA_AD["label"]), esc(ABEMA_AD["imp"])))

    # 関連作品（相互リンク網）
    recs = ((d.get("recommendations") or {}).get("results") or [])
    rel_cards = []
    for r in recs:
        rmedia = r.get("media_type") or media
        if rmedia not in ("movie", "tv"):
            continue
        if not r.get("poster_path"):
            continue
        rtitle = r.get("title") or r.get("name") or ""
        rel_cards.append(
            '<a href="%s/title/%s/%s/"><img loading="lazy" src="%sw185%s" alt="%s"><p>%s</p></a>' % (
                SITE, rmedia, r["id"], IMG, r["poster_path"], esc(rtitle), esc(rtitle)))
        if len(rel_cards) >= 6:
            break
    rel_html = ('<p class="sub">関連作品の配信先</p><div class="rel">%s</div>' % "".join(rel_cards)) if rel_cards else ""

    tags = "".join('<span class="tag">%s</span>' % esc(g) for g in genres)
    meta_bits = []
    if year:
        meta_bits.append('<span class="tag">%s年</span>' % esc(year))
    meta_bits.append('<span class="tag">%s</span>' % type_label)
    if vote:
        meta_bits.append('<span class="tag">★ %.1f</span>' % vote)
    if runtime:
        meta_bits.append('<span class="tag">%s分</span>' % esc(runtime))

    # 構造化データ
    ld = {
        "@context": "https://schema.org",
        "@type": "Movie" if is_movie else "TVSeries",
        "name": title,
        "url": url,
        "description": (overview or summ)[:300],
        "inLanguage": "ja",
    }
    if poster:
        ld["image"] = poster_og
    if date:
        ld["datePublished"] = date
    if genres:
        ld["genre"] = genres
    if vote and vcount:
        ld["aggregateRating"] = {"@type": "AggregateRating", "ratingValue": round(vote, 1),
                                 "bestRating": 10, "ratingCount": vcount}
    ld_json = json.dumps(ld, ensure_ascii=False)

    poster_el = ('<img class="poster" src="%s" alt="%sのポスター">' % (poster, esc(title))) if poster else '<div class="poster"></div>'
    overview_html = ('<p class="sub">あらすじ</p><p class="overview">%s</p>' % esc(overview)) if overview else ""

    return """<!DOCTYPE html><html lang="ja"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>『{title}』はどこで観れる？配信先（Netflix/U-NEXT等）｜dokomiru</title>
<meta name="description" content="{desc}">
<link rel="canonical" href="{url}">
<meta name="theme-color" content="#0b0d12">
<meta property="og:type" content="video.{ogtype}">
<meta property="og:site_name" content="dokomiru">
<meta property="og:title" content="『{title}』はどこで観れる？｜dokomiru">
<meta property="og:description" content="{desc}">
<meta property="og:url" content="{url}">
<meta property="og:image" content="{ogimg}">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:image" content="{ogimg}">
<script type="application/ld+json">{ld}</script>
<script type="application/ld+json">{faq_ld}</script>
<style>{css}</style></head>
<body>
<header><div class="bar"><a class="logo" href="{site}/">dokomiru</a><span style="font-size:.78rem;color:#98a0b3">配信先検索</span></div></header>
<div class="wrap">
<p class="pr-note">※本ページは広告（アフィリエイトリンク）を含みます</p>
<div class="hero">{poster}<div class="h-meta"><h1>『{title}』はどこで観れる？</h1><div class="tags">{metabits}{tags}</div></div></div>
<p class="lead">{summary}</p>
<div class="card"><h2>📺 {title} の配信先（日本）</h2>{avail}</div>
{aff}
{compare}
<a class="cta" href="{site}/?t={media}&id={id}">▶ dokomiruで配信先を開く・お気に入り保存</a>
{abema}
{overview_html}
{cast_html}
{faq_html}
{rel}
<footer>
※本ページは広告（アフィリエイトリンク）を含みます。<br>
配信状況はJustWatch由来で日々変わります。最新・正確な情報は各公式サイトでご確認ください。<br>
配信情報の提供：JustWatch / The Movie Database (TMDB)<br>
<a href="{site}/title/">▶ 配信先がわかる作品一覧</a> ／ <a href="{site}/">dokomiruトップ</a>
</footer>
</div></body></html>""".format(
        title=esc(title), desc=esc(desc), url=esc(url), ogtype=("movie" if is_movie else "tv_show"),
        ogimg=esc(poster_og), ld=ld_json, faq_ld=faq_ld_json, css=CSS, site=SITE, poster=poster_el,
        metabits="".join(meta_bits), tags=tags, summary=esc(summ), avail=avail_html,
        aff=aff_html, compare=compare_html, abema=abema_html, cast_html=cast_html, faq_html=faq_html,
        media=media, id=d["id"], overview_html=overview_html, rel=rel_html)


def write_page(item):
    d = item["detail"]
    media = item["media"]
    is_movie = media == "movie"
    title = (d.get("title") if is_movie else d.get("name")) or ""
    outdir = os.path.join(ROOT, "title", media, str(d["id"]))
    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, "index.html"), "w", encoding="utf-8") as f:
        f.write(render_page(item))
    return {"media": media, "id": d["id"], "title": title,
            "poster": d.get("poster_path"), "year": ((d.get("release_date") if is_movie else d.get("first_air_date")) or "")[:4]}


def render_hub(entries):
    entries_sorted = sorted(entries, key=lambda e: e["title"])
    cards = "".join(
        '<a href="%s/title/%s/%s/"><img loading="lazy" src="%s" alt="%s"><p>%s</p></a>' % (
            SITE, e["media"], e["id"],
            (IMG + "w185" + e["poster"]) if e.get("poster") else "",
            esc(e["title"]), esc(e["title"]))
        for e in entries_sorted)
    return """<!DOCTYPE html><html lang="ja"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>配信先がわかる作品一覧（{n}作品）｜dokomiru</title>
<meta name="description" content="人気の映画・ドラマ{n}作品の配信先（Netflix・U-NEXT・Amazonプライム・Disney+など）を一覧。観たい作品がどこで見放題かすぐわかる。">
<link rel="canonical" href="{site}/title/">
<meta name="theme-color" content="#0b0d12">
<style>{css}</style></head><body>
<header><div class="bar"><a class="logo" href="{site}/">dokomiru</a><span style="font-size:.78rem;color:#98a0b3">作品一覧</span></div></header>
<div class="wrap">
<h1 style="margin:22px 0 6px;font-size:1.3rem">配信先がわかる作品一覧</h1>
<p class="lead">人気の映画・ドラマ {n} 作品。タップすると各作品がどの配信サービスで観られるかわかります。</p>
<div class="rel">{cards}</div>
<footer><a href="{site}/">dokomiruトップへ</a></footer>
</div></body></html>""".format(n=len(entries_sorted), site=SITE, css=CSS, cards=cards)


def write_sitemap(entries):
    urls = ['<url><loc>%s/</loc><lastmod>%s</lastmod><changefreq>daily</changefreq><priority>1.0</priority></url>' % (SITE, TODAY),
            '<url><loc>%s/title/</loc><lastmod>%s</lastmod><changefreq>daily</changefreq><priority>0.8</priority></url>' % (SITE, TODAY)]
    for e in entries:
        urls.append('<url><loc>%s/title/%s/%s/</loc><lastmod>%s</lastmod><changefreq>weekly</changefreq><priority>0.6</priority></url>' % (
            SITE, e["media"], e["id"], TODAY))
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n%s\n</urlset>\n' % "\n".join(urls)
    with open(os.path.join(ROOT, "sitemap.xml"), "w", encoding="utf-8") as f:
        f.write(xml)


def main():
    movie_pages = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    tv_pages = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    print("[1/4] 人気作品IDを収集 (映画%dページ / ドラマ%dページ)..." % (movie_pages, tv_pages))
    movie_ids = discover("movie", movie_pages)
    tv_ids = discover("tv", tv_pages)
    # 今週トレンドを混ぜる（人気順discoverに未掲載の急上昇作品を拾う）
    movie_ids = list(dict.fromkeys(movie_ids + trending("movie")))
    tv_ids = list(dict.fromkeys(tv_ids + trending("tv")))
    targets = [("movie", i) for i in movie_ids] + [("tv", i) for i in tv_ids]
    # 過去に生成済みのページも対象に含める（テンプレ更新を全ページへ波及・sitemap維持）
    seen = set(targets)
    for media in ("movie", "tv"):
        base = os.path.join(ROOT, "title", media)
        if os.path.isdir(base):
            for name in sorted(os.listdir(base)):
                if name.isdigit() and (media, int(name)) not in seen:
                    seen.add((media, int(name)))
                    targets.append((media, int(name)))
    print("    対象: 映画%d / ドラマ%d ＋既存分 = 計%d作品" % (len(movie_ids), len(tv_ids), len(targets)))

    print("[2/4] 詳細＋配信先を取得...")
    items = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        for res in ex.map(lambda t: fetch_one(t[0], t[1]), targets):
            if res:
                items.append(res)
    print("    取得成功: %d作品" % len(items))

    print("[3/4] 静的ページを書き出し...")
    entries = [write_page(it) for it in items]

    print("[4/4] ハブページ＋sitemapを生成...")
    with open(os.path.join(ROOT, "title", "index.html"), "w", encoding="utf-8") as f:
        f.write(render_hub(entries))
    write_sitemap(entries)
    print("完了: %d ページ生成 → title/{movie,tv}/{id}/index.html" % len(entries))


if __name__ == "__main__":
    main()
