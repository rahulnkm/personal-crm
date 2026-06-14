from crm.enrich import parse_payload, EnrichCandidate, ATTRIBUTE, IDENTIFIER


def test_parse_single_object():
    cs = parse_payload('{"field":"location","value":"SF","confidence":0.9,"source":"gravatar"}')
    assert len(cs) == 1 and cs[0].field == "location" and cs[0].kind == ATTRIBUTE


def test_parse_array_and_identifier_kind():
    cs = parse_payload('[{"field":"email","value":"a@b.com","kind":"identifier","confidence":0.9,"source":"gravatar"}]')
    assert cs[0].kind == IDENTIFIER


def test_confidence_validated_range():
    import pytest
    with pytest.raises(ValueError):
        parse_payload('{"field":"location","value":"SF","confidence":1.5,"source":"x"}')


def test_identifier_kind_inferred_from_field():
    cs = parse_payload('{"field":"email","value":"a@b.com","confidence":0.9,"source":"x"}')
    assert cs[0].kind == IDENTIFIER


def test_evidence_folded_into_source_detail():
    cs = parse_payload(
        '{"field":"location","value":"SF","confidence":0.9,"source":"x",'
        '"source_detail":"http://e.com","evidence":"profile says SF"}')
    assert cs[0].source_detail == "http://e.com · profile says SF"
