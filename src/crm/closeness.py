"""Closeness tiers (spec §3.2): the stored tier is a FACT — the most intimate
channel we have evidence for. Channel evidence only ever UPGRADES a tier."""

TIER_RANK = {"t1_irl_messaging": 4, "t2_dm": 3, "t3_community": 2,
             "t4_public": 1, "none": 0}

CHANNEL_TIER = {
    # T1 — met in person / direct messaging
    "irl": "t1_irl_messaging", "imessage": "t1_irl_messaging",
    "whatsapp": "t1_irl_messaging", "telegram": "t1_irl_messaging",
    "signal": "t1_irl_messaging",
    # T2 — platform DMs (a LinkedIn connection/DM is platform-level)
    "linkedin": "t2_dm", "instagram": "t2_dm", "slack_dm": "t2_dm",
    "discord_dm": "t2_dm", "email": "t2_dm",
    # T3 — communities
    "community": "t3_community", "slack": "t3_community", "discord": "t3_community",
    # T4 — public social
    "twitter": "t4_public",
}


def tier_for_channel(channel: str | None) -> str | None:
    if not channel:
        return None
    return CHANNEL_TIER.get(channel.lower())
