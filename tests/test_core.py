"""Offline tests for the deterministic parts of TruthTrace (no network, no AI model)."""
import asyncio
import io

from PIL import Image

from backend.analysis import evidence as ev
from backend.analysis import framing, meaning, media, opinion
from backend.analysis.claims import heuristic_extract, select_tests
from backend.analysis.verify import build_verdict
from backend.textutils import classify_source, extract_numbers, region_of


def item(i, title, snippet="", domain="example.com", source_type="news/web", passage=""):
    return {"id": f"E{i}", "title": title, "snippet": snippet, "passage": passage, "url": f"https://{domain}/{i}", "domain": domain,
            "source": domain, "source_type": source_type, "published": None, "lang": "en", "via": "test", "text": "", "region": "International"}


def test_numbers_ignore_names_and_handle_indian_units():
    vals = [n["value"] for n in extract_numbers("Chandrayaan-3 cost 615 crore and COVID-19 hit 2 lakh people")]
    assert 3 not in vals and 19 not in vals
    assert 615e7 in vals and 2e5 in vals


def test_source_classification():
    assert classify_source("pib.gov.in") == "official"
    assert classify_source("gov.uk") == "official"
    assert classify_source("altnews.in") == "fact-check"
    assert classify_source("pmc.ncbi.nlm.nih.gov") == "academic"
    assert classify_source("en.wikipedia.org") == "reference"
    assert region_of("ndtv.com") == "Indian" and region_of("bbc.com") == "International"


def test_meaning_chain_finds_literal_stage():
    r = asyncio.run(meaning.chain(["The leader's stomach looks swollen.", "The leader looks like a pregnant woman.", "The leader became pregnant."]))
    assert r["first_altered_stage"] == 2
    assert r["stages"][1]["label"] == "Interpretation (opinion)"
    assert r["stages"][2]["label"] == "Distorted literal claim"


def test_meaning_number_negation_certainty():
    ch = lambda a, b: {c["type"] for c in meaning.deterministic_changes(a, b)}
    assert "number changed" in ch("The pilot saved 134 lives.", "The pilot saved 143 lives.")
    assert "negation flipped" in ch("The company did not lay off workers.", "The company laid off workers.")
    assert "certainty increased" in ch("The bridge may collapse.", "The bridge will collapse.")
    assert not meaning.deterministic_changes("The PM praised the pilots.", "The Prime Minister praised the pilots.")


def test_heuristic_stance_polarity_and_questions():
    claim = "The Great Wall of China is visible from space with the naked eye."
    con = ev.heuristic_stance(claim, item(1, "Great Wall not visible from space", "The Great Wall of China is not visible from space with the naked eye, astronauts say."))
    assert con["stance"] == "contradicts"
    q = ev.heuristic_stance(claim, item(2, "Is the Great Wall of China visible from space with the naked eye?"))
    assert q["stance"] != "supports"
    sup = ev.heuristic_stance("Chandrayaan-3 landed near the south pole of the Moon in August 2023.",
                              item(3, "Chandrayaan-3 lands", "Chandrayaan-3 landed near the south pole of the Moon on 23 August 2023."))
    assert sup["stance"] == "supports"


def test_quote_verification_rejects_invented_quotes():
    text = "The ministry said the scheme covers 10 crore families."
    assert ev.quote_found("the scheme covers 10 crore families", text)
    assert not ev.quote_found("the scheme has been cancelled for all families", text)


def test_independence_merges_same_outlet_and_copies():
    a = item(1, "Minister announces new metro line for Pune city", "PTI: The minister announced a new metro line for Pune city on Monday.", "a.com")
    b = item(2, "Minister announces new metro line for Pune city", "PTI: The minister announced a new metro line for Pune city on Monday.", "b.com")
    c = item(3, "Different report", "Officials confirmed budget documents for the metro.", "c.com")
    d = item(4, "Another story", "Unrelated text from the same outlet.", "c.com")
    res = ev.independence([a, b, c, d])
    assert len(res["chains"]) == 2


def test_verdict_rules():
    items = [item(i, "t", source_type="fact-check" if i < 3 else "news/web", domain=f"d{i}.com") for i in range(6)]
    indep = {"chain_of": {it["id"]: it["id"] for it in items}, "wire": {}, "chains": [[it["id"]] for it in items]}
    stances = {it["id"]: {"stance": "contradicts"} for it in items[:4]} | {it["id"]: {"stance": "mentions"} for it in items[4:]}
    time_dim = {"data": {"flags": []}}
    v = build_verdict(items=items, stance_by_id=stances, indep=indep, meaning_result=None, headline_check=None,
                      time_dim=time_dim, media_result=None, ai_mode=False, has_claim=True)
    assert v["verdict"] == "Contradicted"
    none = build_verdict(items=items, stance_by_id={}, indep=indep, meaning_result=None, headline_check=None,
                         time_dim=time_dim, media_result=None, ai_mode=False, has_claim=True)
    assert none["verdict"] == "Insufficient Evidence" and none["confidence"] == "Not applicable"


def test_headline_vs_body():
    body = ("The health ministry said the new drug may reduce symptoms in some patients, according to an early study of 40 people. "
            "Researchers cautioned that larger trials are needed before any conclusion.")
    r = framing.headline_vs_body("Miracle drug cures 90% of patients", body)
    assert r["status"] == "misleading headline"


def test_claim_extraction_and_module_rule():
    info = heuristic_extract("The Great Wall of China is visible from space.", "", "en", None, False)
    assert info["subclaims"][0]["checkable"]
    tests = select_tests(info["misinformation_types"], None)
    assert any("not applicable" in s for s in tests["skipped"])


def test_opinion_counts_people_not_posts():
    posts = [{"platform": "x", "author": "a", "title": "", "text": "visa fee is bad and unfair policy", "url": "", "published": None}] * 3
    posts += [{"platform": "x", "author": "b", "title": "", "text": "the visa fee is a good policy, I support it", "url": "", "published": None}]
    s = opinion.clean_sample(posts, "visa fee policy")
    assert s["exact_duplicates_removed"] == 2 and s["unique_accounts"] == 2


def test_media_image_is_inconclusive_without_provenance():
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), (200, 30, 30)).save(buf, "JPEG")
    r = media.analyse(buf.getvalue(), "photo.jpg", "image/jpeg")
    assert r["label"] == "Media authenticity inconclusive"
    png = io.BytesIO()
    img = Image.new("RGB", (32, 32))
    from PIL import PngImagePlugin
    meta = PngImagePlugin.PngInfo()
    meta.add_text("parameters", "a cat, Steps: 20, Sampler: Euler, Model: Stable Diffusion")
    img.save(png, "PNG", pnginfo=meta)
    r2 = media.analyse(png.getvalue(), "gen.png", "image/png")
    assert r2["label"].startswith("Synthetic media")
