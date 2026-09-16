import pytest
import yaml

from add_route import add_route

ROOT = """# Non-secret behaviour settings
model:
  default: deepseek/deepseek-v4-flash-0731
gateway:
  multiplex_profiles: true
  multiplex_profile_allowlist:
    - monitor
  profile_routes:
    - name: monitor-topic
      platform: telegram
      chat_id: "7850573137"
      thread_id: "5332"
      profile: monitor

# Credentials comment kept
secrets:
  command:
    enabled: true
"""


def test_appends_allowlist_entry_and_route():
    new = add_route(ROOT, "homelab-ops", "7850573137", "6001")
    cfg = yaml.safe_load(new)
    assert cfg["gateway"]["multiplex_profile_allowlist"] == ["monitor", "homelab-ops"]
    assert cfg["gateway"]["profile_routes"][-1] == {
        "name": "homelab-ops-topic", "platform": "telegram",
        "chat_id": "7850573137", "thread_id": "6001", "profile": "homelab-ops"}
    assert "# Credentials comment kept" in new and new.startswith("# Non-secret")


def test_preserves_crlf():
    text = ROOT.replace("\n", "\r\n")
    new = add_route(text, "shopper", "7850573137", "6002")
    assert "\r\n" in new and "\n" not in new.replace("\r\n", "")


def test_second_bot_goes_after_the_first():
    once = add_route(ROOT, "homelab-ops", "7850573137", "6001")
    twice = add_route(once, "shopper", "7850573137", "6002")
    cfg = yaml.safe_load(twice)["gateway"]
    assert cfg["multiplex_profile_allowlist"] == ["monitor", "homelab-ops", "shopper"]
    assert [r["profile"] for r in cfg["profile_routes"]] == ["monitor", "homelab-ops", "shopper"]


@pytest.mark.parametrize("profile, thread, needle", [
    ("monitor", "6001", "already"),
    ("shopper", "5332", "already routed"),
    ("shopper", "12a", "numeric"),
    ("Bad_Name", "6001", "invalid profile name"),
])
def test_refusals(profile, thread, needle):
    with pytest.raises(ValueError, match=needle):
        add_route(ROOT, profile, "7850573137", thread)


def test_refuses_a_config_without_the_blocks():
    with pytest.raises(ValueError, match="not found"):
        add_route("gateway:\n  multiplex_profiles: true\n", "shopper", "7850573137", "6002")
