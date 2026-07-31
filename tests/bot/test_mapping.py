"""Tests for ADR-0009 pane↔thread MappingStore."""

from src.bot.mapping import MappingStore, PaneMapping


def test_persist_and_reload_thread_mapping(tmp_path):
    path = tmp_path / "mapping.json"
    store = MappingStore(path)
    store.set_remote_channel("host-a", 100)
    store.upsert_pane(
        PaneMapping(
            remote_id="host-a",
            pane_id="w1:p1",
            thread_id=200,
            terminal_message_id=300,
            label="agent",
            agent_status="working",
        )
    )

    reloaded = MappingStore(path)
    rm = reloaded.remotes["host-a"]
    assert rm.channel_id == 100
    pm = rm.panes["w1:p1"]
    assert pm.thread_id == 200
    assert pm.terminal_message_id == 300


def test_find_by_thread(tmp_path):
    path = tmp_path / "mapping.json"
    store = MappingStore(path)
    store.upsert_pane(
        PaneMapping(
            remote_id="host-a",
            pane_id="w1:p1",
            thread_id=555,
        )
    )
    found = store.find_by_thread(555)
    assert found is not None
    assert found.pane_id == "w1:p1"


def test_load_legacy_category_and_channel_ids(tmp_path):
    path = tmp_path / "mapping.json"
    path.write_text(
        """
{
  "remotes": {
    "host-a": {
      "category_id": 10,
      "panes": {
        "w1:p1": {
          "remote_id": "host-a",
          "pane_id": "w1:p1",
          "channel_id": 20,
          "terminal_message_id": 30
        }
      }
    }
  }
}
""".strip(),
        encoding="utf-8",
    )
    store = MappingStore(path)
    assert store.remotes["host-a"].channel_id == 10
    assert store.remotes["host-a"].panes["w1:p1"].thread_id == 20
