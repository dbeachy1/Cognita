from cognita.assets.publication import AssetPublisher


def test_prepared_journal_recovery(tmp_path):
    publisher = AssetPublisher(tmp_path / "docs", tmp_path / "data")
    journal = publisher.journal_dir / "stale.json"
    journal.write_text('{"phase":"prepared","operation_id":"stale"}', encoding="utf-8")
    assert publisher.recover() == []
    assert not journal.exists()


def test_uncommitted_published_new_file_is_removed_on_recovery(tmp_path):
    docs, data = tmp_path / "docs", tmp_path / "data"
    docs.mkdir()
    publisher = AssetPublisher(docs, data)
    staged = publisher.stage("crash", b"new")
    published = publisher.publish(staged, "x.png", operation_id="crash")
    assert published.journal.exists() and (docs / "x.png").exists()
    payload = publisher.pending()[0]
    publisher.recover_entry(payload, committed=False)
    assert not (docs / "x.png").exists()
    assert not published.journal.exists()


def test_committed_published_file_survives_recovery(tmp_path):
    docs, data = tmp_path / "docs", tmp_path / "data"
    docs.mkdir()
    publisher = AssetPublisher(docs, data)
    published = publisher.publish(
        publisher.stage("committed", b"new"), "x.png", operation_id="committed"
    )
    publisher.recover_entry(publisher.pending()[0], committed=True)
    assert (docs / "x.png").read_bytes() == b"new"
    assert not published.journal.exists()
