"""AI-mode safeguards, tested with a mocked model: whatever the model says must be traceable to retrieved text."""
import asyncio

from backend import llm
from backend.analysis import evidence as ev
from backend.analysis import influencers, perspectives


def _item(i, text, source_type="news/web"):
    return {"id": f"E{i}", "title": f"Title {i}", "snippet": text, "passage": text, "text": text, "url": f"https://s{i}.com/a",
            "domain": f"s{i}.com", "source": f"s{i}.com", "source_type": source_type, "published": "2024-01-01T00:00:00+00:00",
            "lang": "en", "via": "test", "region": "International"}


def _mock(monkeypatch, reply):
    async def fake(task, prompt, max_tokens=4000):
        return reply
    monkeypatch.setattr(llm, "enabled", lambda: True)
    monkeypatch.setattr(llm, "ask_json", fake)


def test_fabricated_stance_quote_is_not_counted(monkeypatch):
    items = [_item(1, "Officials confirmed the dam was completed in 2019."), _item(2, "The dam project faced delays.")]
    _mock(monkeypatch, {"assessments": [
        {"evidence_id": "E1", "subclaim": 0, "stance": "supports", "quote": "Officials confirmed the dam was completed in 2019.", "reason": "direct"},
        {"evidence_id": "E2", "subclaim": 0, "stance": "contradicts", "quote": "The dam was never built and the claim is false.", "reason": "invented"},
        {"evidence_id": "E99", "subclaim": 0, "stance": "supports", "quote": "x", "reason": "unknown id"},
    ]})
    out = {s["evidence_id"]: s for s in asyncio.run(ev.assess("The dam was completed in 2019.", [], items))}
    assert out["E1"]["stance"] == "supports" and out["E1"]["quote_verified"]
    assert out["E2"]["stance"] == "mentions"          # invented quote → stance discarded
    assert "could not be found" in out["E2"]["reason"]
    assert "E99" not in out                          # evidence that was never retrieved cannot appear


def test_perspectives_drop_unknown_evidence_ids(monkeypatch):
    items = [_item(1, "The port will handle 16 million containers a year, the ministry said.", "official")]
    _mock(monkeypatch, {
        "verifiable_facts": [{"fact": "Port capacity 16 million containers", "evidence_ids": ["E1"]},
                             {"fact": "Invented fact", "evidence_ids": ["E42"]}],
        "viewpoints": [{"label": "Supporters", "summary": "", "arguments": [{"argument": "trade", "evidence_ids": ["E1", "E7"],
                        "factual_claims": [{"claim": "boosts trade", "status": "supported", "evidence_ids": ["E7"]}]}]}],
        "reasoned_conclusion": {"conclusion": "Evidence is insufficient to conclude.", "confidence": "low"},
    })
    r = asyncio.run(perspectives.analyse("port project", items))
    assert [f["fact"] for f in r["verifiable_facts"]] == ["Port capacity 16 million containers"]
    arg = r["viewpoints"][0]["arguments"][0]
    assert arg["evidence_ids"] == ["E1"]
    assert arg["factual_claims"][0]["status"] == "unverified"   # 'supported' with no real evidence is downgraded


def test_influencer_quotes_must_exist_in_source(monkeypatch):
    items = [_item(1, '"We will cut tariffs next month," Commerce Minister Piyush Goyal said on Monday.')]
    _mock(monkeypatch, {"statements": [
        {"speaker": "Piyush Goyal", "quote": "We will cut tariffs next month", "evidence_id": "E1", "factual_claims": []},
        {"speaker": "Piyush Goyal", "quote": "Tariffs will be abolished forever", "evidence_id": "E1", "factual_claims": []},
    ]})
    st = asyncio.run(influencers.ai_statements(items, "tariffs"))
    assert [s["quote"] for s in st] == ["We will cut tariffs next month"]
