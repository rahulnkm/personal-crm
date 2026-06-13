from crm.closeness import CHANNEL_TIER, TIER_RANK, tier_for_channel


def test_rank_ordering():
    assert TIER_RANK["t1_irl_messaging"] > TIER_RANK["t2_dm"] > \
        TIER_RANK["t3_community"] > TIER_RANK["t4_public"] > TIER_RANK["none"]


def test_channel_mapping():
    assert tier_for_channel("imessage") == "t1_irl_messaging"
    assert tier_for_channel("irl") == "t1_irl_messaging"
    assert tier_for_channel("linkedin") == "t2_dm"
    assert tier_for_channel("unknown-channel") is None
    assert tier_for_channel(None) is None
