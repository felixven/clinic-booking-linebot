def parse_ticket_ids(raw_ticket_ids):
    """
    將各種可能型態的 ticketIds 轉成 list[int]

    支援：
    - [1140, 1139]
    - ["1140", "1139"]
    - "1140,1139"
    - "1140"
    - None / "" / 不合法 → []
    """
    if not raw_ticket_ids:
        return []

    # case 1: list / tuple / set
    if isinstance(raw_ticket_ids, (list, tuple, set)):
        ids = []
        for x in raw_ticket_ids:
            try:
                ids.append(int(x))
            except Exception:
                continue
        return ids

    # case 2: string "1140,1139"
    if isinstance(raw_ticket_ids, str):
        parts = raw_ticket_ids.split(",")
        ids = []
        for p in parts:
            p = p.strip()
            if not p:
                continue
            try:
                ids.append(int(p))
            except Exception:
                continue
        return ids

    # fallback
    return []
