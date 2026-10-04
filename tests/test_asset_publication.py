from cognita.assets.publication import AssetPublisher


def test_publisher_creates_only_final_path(tmp_path):
    docs, data = tmp_path / "docs", tmp_path / "data"
    docs.mkdir()
    publisher = AssetPublisher(docs, data)
    staged = publisher.stage("op", b"payload")
    published = publisher.publish(staged, "x.png", operation_id="op")
    assert (docs / "x.png").read_bytes() == b"payload"
    assert published.journal.is_file()
    assert staged.is_file()
    publisher.commit(published)
    assert not list((data / "assets" / "staging").glob("*"))
    assert not list(publisher.journal_dir.glob("*"))


def test_publisher_rollback_restores_prior_bytes(tmp_path):
    docs, data = tmp_path / "docs", tmp_path / "data"
    docs.mkdir()
    (docs / "x.png").write_bytes(b"old")
    publisher = AssetPublisher(docs, data)
    staged = publisher.stage("replace", b"new")
    published = publisher.publish(staged, "x.png", operation_id="replace", overwrite=True)
    assert (docs / "x.png").read_bytes() == b"new"
    publisher.rollback(published)
    assert (docs / "x.png").read_bytes() == b"old"
    assert not published.journal.exists()


def test_connector_identity_separates_shared_staging_and_recovery_journals(tmp_path):
    docs, data = tmp_path / "docs", tmp_path / "data"
    docs.mkdir()
    publisher = AssetPublisher(docs, data)

    staged_a = publisher.stage("same-op", b"a", connector_id="connector-a")
    staged_b = publisher.stage("same-op", b"b", connector_id="connector-b")
    staged_c = publisher.stage("same-op", b"c", connector_id="connector-a", tool="update_asset_metadata")
    assert staged_a != staged_b
    assert staged_a != staged_c

    published_a = publisher.publish(
        staged_a, "a.png", operation_id="same-op", connector_id="connector-a",
        tool="put_asset",
    )
    published_b = publisher.publish(
        staged_b, "b.png", operation_id="same-op", connector_id="connector-b",
        tool="put_asset",
    )
    pending = {item["filepath"]: item for item in publisher.pending()}
    assert pending["a.png"]["connector_id"] == "connector-a"
    assert pending["b.png"]["connector_id"] == "connector-b"
    assert pending["a.png"]["tool"] == pending["b.png"]["tool"] == "put_asset"

    publisher.commit(published_a)
    publisher.commit(published_b)
    publisher.cleanup(staged_c)
