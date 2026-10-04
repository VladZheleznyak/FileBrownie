from contextlib import contextmanager

import pytest
from support import FakeVision, pdf_lines

from filebrownie.evidence.normalize import stems
from filebrownie.ingestion.scan import run_full_scan
from filebrownie.presentation import cli
from filebrownie.query.dictionary import build, label_key, load_seed, stem_key


@pytest.fixture(scope="module")
def seed():
    return load_seed()[0]


def dictionary(seed, **kwargs):
    return build(seed, 1, **kwargs)


def test_seed_resolves_all_three_languages_to_one_concept(seed):
    d = dictionary(seed)
    for term in ("Hemoglobin", "ГЕМОГЛОБИН", "Гемоглобін", "hgb"):
        assert d.resolve(term, "analyte").concepts == {"hemoglobin"}
    for term in ("urologist", "Уролог", "урологія"):
        assert d.resolve(term, "specialty").concepts == {"urology"}


def test_groups_expand_to_members_and_keep_all_synonyms(seed):
    d = dictionary(seed)
    scope = d.resolve("iron panel", "analyte")
    assert scope.concepts == {"ferritin", "serum-iron", "tibc", "transferrin-saturation"}
    assert scope.group == "iron panel"
    assert stems("ферритин") in {stem_key(key) for key in scope.synonyms}
    eyes = d.resolve("eye care", "specialty")
    assert eyes.concepts == {"optometry", "ophthalmology"}


def test_exact_match_is_mapped_and_inflection_only_is_a_candidate(seed):
    d = dictionary(seed)
    scope = d.resolve("уролог", "specialty")
    assert d.match("Уролог", scope).status == "mapped"
    for inflected in ("уролога", "урологу", "Урологом", "УРОЛОГА"):
        assert d.match(inflected, scope).status == "inflected"
    assert d.match("Окулист", scope) is None
    assert d.match("urologists", d.resolve("urologist", "specialty")).status == "inflected"


def test_unknown_term_still_matches_raw_labels_but_not_confirmed_concepts(seed):
    d = dictionary(seed)
    scope = d.resolve("Soluble transferrin receptor", "analyte")
    assert not scope.concepts
    assert d.match("soluble transferrin receptor", scope).status == "raw"
    assert d.match("Albumin", scope) is None


def test_proposals_and_acceptance_and_rejection(seed):
    d = dictionary(seed)
    assert d.propose("Ferritin, serum", "analyte") == ["ferritin"]
    assert d.propose("Ferritin", "analyte") == []  # already mapped exactly
    assert d.propose("ферритина", "analyte") == []  # inflection-only is a query-time candidate
    key = ("ferritin", "serum")
    scope = d.resolve("ferritin", "analyte")
    assert d.match("Ferritin, serum", scope) is None  # no proposal stored in this dictionary
    with_proposal = dictionary(seed, proposals=[(key, "ferritin")])
    assert (
        with_proposal.match("Ferritin, serum", with_proposal.resolve("ferritin", "analyte")).status
        == "auto-mapped"
    )
    accepted = dictionary(seed, accepted=[(key, "ferritin")])
    assert (
        accepted.match("Ferritin, serum", accepted.resolve("ferritin", "analyte")).status
        == "mapped"
    )
    rejected = dictionary(seed, proposals=[(key, "ferritin")], rejected=[(key, "ferritin")])
    assert rejected.match("Ferritin, serum", rejected.resolve("ferritin", "analyte")) is None
    assert rejected.propose("Ferritin, serum", "analyte") == []


def test_group_id_with_hyphen_resolves_members(seed):
    d = dictionary(seed)
    scope = d.resolve("iron-panel", "analyte")
    assert scope.group == "iron panel"
    assert scope.concepts == frozenset({"ferritin", "serum-iron", "tibc", "transferrin-saturation"})


def test_rejected_concept_pair_does_not_confirm_as_raw(seed):
    key = label_key("Ferritin")
    rejected = dictionary(seed, rejected=[(key, "ferritin")])
    scope = rejected.resolve("ferritin", "analyte")
    assert rejected.match("Ferritin", scope) is None


def test_ambiguous_exact_label_is_a_candidate_for_every_meaning(seed):
    d = dictionary(seed, accepted=[(("fe",), "ferritin")])
    for term in ("ferritin", "serum iron"):
        scope = d.resolve(term, "analyte")
        assert d.match("Fe", scope).status == "ambiguous"


@pytest.fixture
def folders(tmp_path, repository):
    source, data = tmp_path / "sources", tmp_path / "data"
    source.mkdir()
    data.mkdir()
    repository.migrate()
    return source, data


@pytest.mark.integration
def test_seed_sync_is_idempotent_and_revision_moves_with_decisions(repository, folders):
    store = repository.dictionary
    first = store.revision()
    assert first >= 1 and store.sync_seed() is False
    store.decide("Ferritin, serum", "ferritin", "accepted")
    assert store.revision() == first + 1
    assert store.reverse("Ferritin, serum", "ferritin") and store.revision() == first + 2
    assert not store.reverse("Ferritin, serum", "ferritin")


@pytest.mark.integration
def test_scan_proposals_are_generation_owned_and_isolated_until_activation(repository, folders):
    source, data = folders
    pdf_lines(
        source / "labs.pdf", [(20, 40, "Ferritin, serum"), (200, 40, "12"), (280, 40, "ng/mL")]
    )
    vision = FakeVision(
        {"lab_rows": [{"label": "Ferritin, serum", "value": "12", "unit": "ng/mL"}]}
    )
    first = run_full_scan(repository, source, data, None, vision)
    store = repository.dictionary
    assert [(p["label"], p["concept_id"]) for p in store.proposals(first.generation_id)] == [
        ("Ferritin, serum", "ferritin")
    ]
    active = store.snapshot(repository.active_generation_id())
    assert (
        active.match("Ferritin, serum", active.resolve("ferritin", "analyte")).status
        == "auto-mapped"
    )
    # A staged scan's proposals never affect lookup.
    (source / "extra.pdf").write_bytes(b"synthetic malformed")
    staged = run_full_scan(repository, source, data, None, vision)
    assert not staged.activation.activated
    still = store.snapshot(repository.active_generation_id())
    assert repository.active_generation_id() == first.generation_id
    assert still.proposals == active.proposals
    # Durable rejection survives a rescan: cached output cannot restore the pair.
    store.decide("Ferritin, serum", "ferritin", "rejected")
    (source / "extra.pdf").unlink()
    again = run_full_scan(repository, source, data, None, vision)
    assert store.proposals(again.generation_id) == []
    after = store.snapshot(repository.active_generation_id())
    assert after.match("Ferritin, serum", after.resolve("ferritin", "analyte")) is None


@pytest.mark.integration
def test_dict_cli_review_accept_reject_reverse(repository, folders, monkeypatch, capsys):
    source, data = folders
    pdf_lines(source / "labs.pdf", [(20, 40, "Ferritin, serum"), (200, 40, "12")])
    vision = FakeVision({"lab_rows": [{"label": "Ferritin, serum", "value": "12"}]})
    run_full_scan(repository, source, data, None, vision)
    monkeypatch.setenv("FILEBROWNIE_SOURCE_DIR", str(source))
    monkeypatch.setenv("FILEBROWNIE_DATA_DIR", str(data))

    @contextmanager
    def local():
        yield repository

    monkeypatch.setattr(cli, "open_repository", local)
    assert cli.main(["dict", "review"]) == 0
    assert "Ferritin, serum" in capsys.readouterr().out
    assert cli.main(["dict", "accept", "Ferritin, serum", "ferritin"]) == 0
    assert cli.main(["dict", "review"]) == 0
    output = capsys.readouterr().out
    assert "Unreviewed proposals (auto-mapped, unreviewed): 0" in output and "accepted" in output
    assert cli.main(["dict", "accept", "x", "no-such-concept"]) == 2
    assert cli.main(["dict", "reject", "Ferritin, serum", "ferritin"]) == 0
    assert cli.main(["dict", "reverse", "Ferritin, serum", "ferritin"]) == 0
    assert cli.main(["dict", "reverse", "Ferritin, serum", "ferritin"]) == 1
