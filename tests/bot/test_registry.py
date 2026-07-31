from src.bot.registry import RemoteRecord, RemoteRegistry


def test_unbind_keeps_credentials(tmp_path):
    reg = RemoteRegistry(tmp_path / "r.json")
    reg.upsert(
        RemoteRecord(
            id="r1",
            host="1.2.3.4",
            port=8787,
            token="t",
            fingerprint="a" * 64,
            channel_id=1,
        )
    )
    reg.unbind_channel("r1")
    r = reg.get("r1")
    assert r.channel_id is None and r.token == "t"
    assert "r1" in [x.id for x in reg.list_unbound()]


def test_bind_and_list_unbound(tmp_path):
    reg = RemoteRegistry(tmp_path / "r.json")
    reg.upsert(
        RemoteRecord(
            id="r1",
            host="1.2.3.4",
            port=8787,
            token="t",
            fingerprint="a" * 64,
            channel_id=None,
        )
    )
    assert reg.list_unbound() == [reg.get("r1")]
    reg.bind_channel("r1", 99)
    assert reg.get("r1").channel_id == 99
    assert reg.list_unbound() == []


def test_remove(tmp_path):
    reg = RemoteRegistry(tmp_path / "r.json")
    reg.upsert(
        RemoteRecord(
            id="r1",
            host="1.2.3.4",
            port=8787,
            token="t",
            fingerprint="a" * 64,
            channel_id=None,
        )
    )
    reg.remove("r1")
    assert reg.get("r1") is None
